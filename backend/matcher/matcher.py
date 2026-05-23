"""EventMatcher — links Polymarket events to ExternalEvent rows.

Runs as a long-lived asyncio task in the FastAPI lifespan. Each cycle:
  1. Loads the cached Polymarket tree.
  2. Walks every event with a parseable head-to-head title.
  3. For each one, queries `external_events` for the same sport in a time
     window around the PM event's start, then scores every candidate with
     rapidfuzz (best-of either home/away ordering).
  4. Upserts the best candidate per (polymarket_event_id, source) into
     `event_matches` when `score >= MATCH_THRESHOLD`.

The matcher is conservative: it only emits high-confidence links. Bad matches
poison downstream EV math, so we'd rather skip than guess.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import and_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.matcher.normalize import extract_teams_from_title, normalize_team
from backend.models import EventMatch, ExternalEvent, PolymarketTreeCache

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.72            # combined score lower bound
NAME_BYPASS_THRESHOLD = 0.85      # if name match alone is this strong, accept regardless of timing
TIME_WINDOW_MINUTES = 12 * 60     # ± window around PM event for candidate lookup
TIME_PENALTY_FULL_AT_MIN = 720.0  # |Δt| ≥ 12h ⇒ tempo_score = 0 (matches the hard window)
RUN_INTERVAL_S = 60.0             # seconds between cycles
# PM lists events weeks/months out; Pinnacle only the next couple of weeks.
# Don't attempt matches outside this horizon — they'd never resolve and they
# drag coverage_pct down without signal.
PM_HORIZON_DAYS = 14


@dataclass
class MatcherStats:
    last_run_at: datetime | None = None
    last_run_duration_ms: float | None = None
    total_runs: int = 0
    last_pm_events_scanned: int = 0
    last_pm_events_parseable: int = 0
    last_pm_events_matchable: int = 0   # parseable AND has >=1 candidate in window
    last_matches_written: int = 0
    last_matches_total: int = 0
    # coverage_pct = matched / matchable. Better signal than matched / parseable
    # because external coverage is the real bottleneck (Pinnacle only lists 1-2
    # days ahead for many leagues, so distant PM events have no candidates).
    coverage_pct: float = 0.0
    coverage_by_sport: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_run_duration_ms": self.last_run_duration_ms,
            "total_runs": self.total_runs,
            "last_pm_events_scanned": self.last_pm_events_scanned,
            "last_pm_events_parseable": self.last_pm_events_parseable,
            "last_pm_events_matchable": self.last_pm_events_matchable,
            "last_matches_written": self.last_matches_written,
            "last_matches_total": self.last_matches_total,
            "coverage_pct": self.coverage_pct,
            "coverage_by_sport": self.coverage_by_sport,
        }


@dataclass(slots=True)
class _PMEvent:
    event_id: str
    sport: str
    title: str
    home: str
    away: str
    start_time: datetime


def _score(
    pm_home: str,
    pm_away: str,
    ext_home: str,
    ext_away: str,
    pm_start: datetime,
    ext_start: datetime,
) -> tuple[float, float, float, float]:
    """Return (combined, home_score, away_score, |Δt minutes|).

    Best-of-orderings: PM doesn't reliably encode which side is home, so we
    score both (pm_home↔ext_home, pm_away↔ext_away) and
    (pm_home↔ext_away, pm_away↔ext_home) and take the max.
    """

    def s(a: str, b: str) -> float:
        # partial_token_set_ratio is forgiving of one side carrying extra
        # qualifiers (e.g. PM "raja athletic" vs Pinnacle "raja casablanca",
        # "fk polissia" vs "polissya zhytomyr") while keeping unrelated names
        # well below the bypass threshold.
        return fuzz.partial_token_set_ratio(a, b) / 100.0

    same = (s(pm_home, ext_home) + s(pm_away, ext_away)) / 2.0
    swap = (s(pm_home, ext_away) + s(pm_away, ext_home)) / 2.0
    if same >= swap:
        home_score = s(pm_home, ext_home)
        away_score = s(pm_away, ext_away)
    else:
        home_score = s(pm_home, ext_away)
        away_score = s(pm_away, ext_home)

    delta_min = abs((pm_start - ext_start).total_seconds()) / 60.0
    tempo = max(0.0, 1.0 - delta_min / TIME_PENALTY_FULL_AT_MIN)

    # Geometric mean of name scores — both sides must be reasonable.
    name_score = (home_score * away_score) ** 0.5
    # Strong name matches survive bad timing (different sources publish in
    # different timezones / sometimes hours off). Weaker name matches get
    # the timing as a tiebreaker.
    if name_score >= NAME_BYPASS_THRESHOLD:
        combined = name_score
    else:
        combined = name_score * (0.6 + 0.4 * tempo)
    return combined, home_score, away_score, delta_min


def _walk_tree_events(tree: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
    """Flatten tree to [(event_id, sport_id, title, end_date_iso), ...]."""
    out: list[tuple[str, str, str, str | None]] = []
    for sport in tree.get("sports", []) or []:
        sid = sport.get("id") or ""
        for league in sport.get("leagues", []) or []:
            for ev in league.get("events", []) or []:
                eid = str(ev.get("id") or "")
                if not eid:
                    continue
                title = ev.get("title") or ""
                # Tree node stores `end_date` (game time approximated); fall
                # back to start_date if missing.
                end_iso = ev.get("end_date") or ev.get("start_date")
                out.append((eid, sid, title, end_iso))
    return out


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip — coerce all matcher datetimes to
    offset-aware UTC so comparisons don't blow up."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Polymarket sport ids (from polymarket/tree.py KNOWN_SPORT_MAP) → which
# Pinnacle/external sport buckets are acceptable matches. Keeps soccer
# matches out of cricket etc.
_SPORT_BRIDGES: dict[str, set[str]] = {
    "soccer": {"soccer"},
    "nba": {"nba"},
    "basketball-generic": {"nba"},
    "mlb": {"mlb"},
    "nhl": {"nhl"},
    "hockey-generic": {"nhl"},
    "nfl": {"nfl"},
    "tennis": {"tennis"},
    "mma": {"mma"},
    "boxing": {"boxing"},
    "f1": {"f1"},
    "cricket": {"cricket"},
    "esports": {"esports"},
    "golf": {"golf"},
}


class EventMatcher:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.stats = MatcherStats()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("EventMatcher starting (interval=%.0fs)", RUN_INTERVAL_S)
        while not self._stop.is_set():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EventMatcher cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RUN_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("EventMatcher stopped")

    async def _run_cycle(self) -> None:
        started = time.time()
        async with self.session_factory() as session:
            row = (
                await session.execute(select(PolymarketTreeCache).where(PolymarketTreeCache.id == 1))
            ).scalar_one_or_none()
            if row is None:
                logger.info("EventMatcher: no PM tree cached, skipping cycle")
                return
            try:
                tree = json.loads(row.payload)
            except json.JSONDecodeError:
                logger.warning("EventMatcher: corrupted tree cache payload")
                return

            raw_events = _walk_tree_events(tree)
            self.stats.last_pm_events_scanned = len(raw_events)

            horizon = datetime.now(UTC) + timedelta(days=PM_HORIZON_DAYS)
            pm_events: list[_PMEvent] = []
            for eid, sport, title, end_iso in raw_events:
                teams = extract_teams_from_title(title)
                if not teams:
                    continue
                t = _parse_iso(end_iso)
                if t is None or t > horizon:
                    continue
                home, away = teams
                pm_events.append(
                    _PMEvent(
                        event_id=eid,
                        sport=sport,
                        title=title,
                        home=home,
                        away=away,
                        start_time=t,
                    )
                )
            self.stats.last_pm_events_parseable = len(pm_events)

            written, matchable_ids = await self._match_and_write(session, pm_events)
            self.stats.last_matches_written = written
            self.stats.last_pm_events_matchable = len(matchable_ids)

            # Coverage: how many *matchable* PM events ended up with at least
            # one match in the table (across all sources). "matchable" means
            # there's at least one external_event candidate inside the time
            # window for that sport — events with no candidates can't possibly
            # match and shouldn't drag the metric down.
            total_matches_q = await session.execute(select(EventMatch))
            all_matches = total_matches_q.scalars().all()
            self.stats.last_matches_total = len(all_matches)

            matched_polyev_ids = {m.polymarket_event_id for m in all_matches}
            by_sport_counts: dict[str, dict[str, int]] = defaultdict(
                lambda: {"parseable": 0, "matchable": 0, "matched": 0}
            )
            matched_matchable = 0
            for ev in pm_events:
                bucket = by_sport_counts[ev.sport]
                bucket["parseable"] += 1
                if ev.event_id in matchable_ids:
                    bucket["matchable"] += 1
                if ev.event_id in matched_polyev_ids:
                    bucket["matched"] += 1
                    if ev.event_id in matchable_ids:
                        matched_matchable += 1
            self.stats.coverage_by_sport = dict(by_sport_counts)
            self.stats.coverage_pct = (
                100.0 * matched_matchable / len(matchable_ids) if matchable_ids else 0.0
            )

        self.stats.last_run_at = datetime.now(UTC)
        self.stats.last_run_duration_ms = (time.time() - started) * 1000.0
        self.stats.total_runs += 1
        logger.info(
            "EventMatcher cycle: scanned=%d parseable=%d matchable=%d written=%d total=%d coverage=%.1f%% in %.0fms",
            self.stats.last_pm_events_scanned,
            self.stats.last_pm_events_parseable,
            self.stats.last_pm_events_matchable,
            written,
            self.stats.last_matches_total,
            self.stats.coverage_pct,
            self.stats.last_run_duration_ms,
        )

    async def _match_and_write(
        self, session, pm_events: list[_PMEvent]
    ) -> tuple[int, set[str]]:
        """Return (rows_written, polymarket_event_ids_with_candidates_in_window)."""
        if not pm_events:
            return 0, set()

        # Pull every ExternalEvent within the full time horizon (one query
        # rather than one per PM event). Bucket by sport for fast lookup.
        min_t = min(ev.start_time for ev in pm_events) - timedelta(minutes=TIME_WINDOW_MINUTES)
        max_t = max(ev.start_time for ev in pm_events) + timedelta(minutes=TIME_WINDOW_MINUTES)
        ext_rows = (
            await session.execute(
                select(ExternalEvent).where(
                    and_(
                        ExternalEvent.start_time >= min_t,
                        ExternalEvent.start_time <= max_t,
                    )
                )
            )
        ).scalars().all()

        by_sport: dict[str, list[tuple[ExternalEvent, datetime]]] = defaultdict(list)
        for r in ext_rows:
            by_sport[r.sport or ""].append((r, _as_utc(r.start_time)))

        written = 0
        matchable_ids: set[str] = set()
        for ev in pm_events:
            allowed_sports = _SPORT_BRIDGES.get(ev.sport)
            if not allowed_sports:
                continue
            best_by_source: dict[str, tuple[ExternalEvent, float, float, float, float]] = {}
            window_lo = ev.start_time - timedelta(minutes=TIME_WINDOW_MINUTES)
            window_hi = ev.start_time + timedelta(minutes=TIME_WINDOW_MINUTES)
            has_candidate = False
            for sport_id in allowed_sports:
                for cand, cand_start in by_sport.get(sport_id, ()):
                    if not (window_lo <= cand_start <= window_hi):
                        continue
                    has_candidate = True
                    cand_home = normalize_team(cand.home_team)
                    cand_away = normalize_team(cand.away_team)
                    if not cand_home or not cand_away:
                        continue
                    combined, hs, as_, dt_min = _score(
                        ev.home, ev.away, cand_home, cand_away, ev.start_time, cand_start
                    )
                    if combined < MATCH_THRESHOLD:
                        continue
                    prev = best_by_source.get(cand.source)
                    if prev is None or combined > prev[1]:
                        best_by_source[cand.source] = (cand, combined, hs, as_, dt_min)
            if has_candidate:
                matchable_ids.add(ev.event_id)

            for source, (cand, combined, hs, as_, dt_min) in best_by_source.items():
                stmt = sqlite_insert(EventMatch).values(
                    polymarket_event_id=ev.event_id,
                    external_event_id=cand.id,
                    source=source,
                    score=combined,
                    home_score=hs,
                    away_score=as_,
                    time_delta_minutes=dt_min,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        EventMatch.polymarket_event_id,
                        EventMatch.source,
                    ],
                    set_={
                        "external_event_id": cand.id,
                        "score": combined,
                        "home_score": hs,
                        "away_score": as_,
                        "time_delta_minutes": dt_min,
                        "updated_at": datetime.now(UTC),
                    },
                )
                await session.execute(stmt)
                written += 1
        await session.commit()
        return written, matchable_ids
