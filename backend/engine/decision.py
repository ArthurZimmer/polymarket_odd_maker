"""DecisionEngine — periodic dry-run evaluator.

Each cycle:
  1. Iterates every persisted PM event with at least one EventMatch.
  2. For each matched PM token, finds the latest poly snapshot + latest
     Pinnacle snapshot for the corresponding outcome side.
  3. Computes a devigged fair prob from Pinnacle and an EV vs Polymarket ask.
  4. Applies the gating filters (window, liquidity, EV threshold, sanity).
  5. Logs every evaluation into `decision_log` — BUY *or* PASS_*.

The engine never sends an order. Live trading lands in Etapa 9; until then
this is the audit trail used to tune thresholds and sanity-check matches.
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

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.engine.ev import (
    MAX_FAIR_PROB,
    MIN_FAIR_PROB,
    compute_ev_buy,
    consensus_fair_prob,
    devig_simple,
    implied_prob_from_decimal,
    map_pm_outcome_to_side,
)
from backend.models import (
    BotState,
    DecisionLog,
    EventMatch,
    ExternalEvent,
    OddsSnapshot,
    PolymarketToken,
    PolymarketTreeCache,
)

logger = logging.getLogger(__name__)


# Defaults — used only when no BotState row exists yet. Otherwise every
# threshold is read from BotState each cycle via _RuntimeKnobs.
DEFAULT_EV_THRESHOLD = 0.03
DEFAULT_MIN_TIME_TO_GAME_S = 5 * 60
DEFAULT_MAX_TIME_TO_GAME_S = 120 * 60
DEFAULT_MIN_ASK_DEPTH_USD = 100.0
DEFAULT_MASTER_STAKE_USD = 10.0

SNAPSHOT_MAX_AGE_S = 90.0           # poly/ext snapshot can't be older than this (not user-tunable)
RUN_INTERVAL_S = 10.0               # how often the engine cycles


@dataclass(slots=True)
class _RuntimeKnobs:
    """Snapshot of BotState knobs at the start of a DecisionEngine cycle.

    Read once per cycle to keep behaviour consistent across the iteration
    and so the user's slider changes in /config/risk take effect on the
    next tick (instead of the engine using the constants it was imported
    with).
    """
    ev_threshold: float
    min_time_to_game_s: float
    max_time_to_game_s: float
    min_ask_depth_usd: float
    master_stake_usd: float

    @classmethod
    def from_state(cls, state: BotState | None) -> "_RuntimeKnobs":
        if state is None:
            return cls(
                ev_threshold=DEFAULT_EV_THRESHOLD,
                min_time_to_game_s=DEFAULT_MIN_TIME_TO_GAME_S,
                max_time_to_game_s=DEFAULT_MAX_TIME_TO_GAME_S,
                min_ask_depth_usd=DEFAULT_MIN_ASK_DEPTH_USD,
                master_stake_usd=DEFAULT_MASTER_STAKE_USD,
            )
        return cls(
            ev_threshold=state.ev_threshold,
            min_time_to_game_s=state.min_time_to_game_minutes * 60,
            max_time_to_game_s=state.max_time_to_game_minutes * 60,
            min_ask_depth_usd=state.min_ask_depth_usd,
            master_stake_usd=state.master_stake_usd,
        )


# Decision action codes — kept short for easy filtering in the UI.
class Action:
    BUY = "BUY"
    PASS_LOW_EV = "PASS_LOW_EV"
    PASS_WINDOW_EARLY = "PASS_WINDOW_EARLY"   # event > MAX_TIME_TO_GAME
    PASS_WINDOW_LATE = "PASS_WINDOW_LATE"     # event < MIN_TIME_TO_GAME
    PASS_LIQUIDITY = "PASS_LIQUIDITY"
    PASS_NO_MATCH = "PASS_NO_MATCH"
    PASS_NO_POLY_SNAP = "PASS_NO_POLY_SNAP"
    PASS_NO_EXT_SNAP = "PASS_NO_EXT_SNAP"
    PASS_DEVIG_FAILED = "PASS_DEVIG_FAILED"
    PASS_NO_MAP = "PASS_NO_MAP"
    PASS_FAIR_BOUNDS = "PASS_FAIR_BOUNDS"
    ERROR = "ERROR"


@dataclass
class EngineStats:
    last_run_at: datetime | None = None
    last_run_duration_ms: float | None = None
    total_runs: int = 0
    last_evaluations: int = 0
    last_buys: int = 0
    last_passes_by_reason: dict[str, int] = field(default_factory=dict)
    total_buys: int = 0
    total_passes: int = 0
    total_decisions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_run_duration_ms": self.last_run_duration_ms,
            "total_runs": self.total_runs,
            "last_evaluations": self.last_evaluations,
            "last_buys": self.last_buys,
            "last_passes_by_reason": self.last_passes_by_reason,
            "total_buys": self.total_buys,
            "total_passes": self.total_passes,
            "total_decisions": self.total_decisions,
        }


@dataclass(slots=True)
class _PMEventTitle:
    sport: str
    league: str
    title: str


def _walk_tree_for_titles(tree: dict[str, Any]) -> dict[str, _PMEventTitle]:
    out: dict[str, _PMEventTitle] = {}
    for sport in tree.get("sports", []) or []:
        sid = sport.get("id") or ""
        for league in sport.get("leagues", []) or []:
            lbl = league.get("label") or ""
            for ev in league.get("events", []) or []:
                eid = str(ev.get("id") or "")
                if not eid:
                    continue
                out[eid] = _PMEventTitle(
                    sport=sid, league=lbl, title=ev.get("title") or ""
                )
    return out


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class DecisionEngine:
    def __init__(self, session_factory, *, dry_run: bool = True) -> None:
        self.session_factory = session_factory
        self.dry_run = dry_run
        self.stats = EngineStats()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info(
            "DecisionEngine starting (dry_run=%s, interval=%.0fs, default_ev>=%.0f%%)",
            self.dry_run,
            RUN_INTERVAL_S,
            DEFAULT_EV_THRESHOLD * 100,
        )
        while not self._stop.is_set():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("DecisionEngine cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RUN_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("DecisionEngine stopped")

    async def _run_cycle(self) -> None:
        started = time.time()
        passes: dict[str, int] = defaultdict(int)
        n_eval = 0
        n_buy = 0

        async with self.session_factory() as session:
            state = (
                await session.execute(select(BotState).where(BotState.id == 1))
            ).scalar_one_or_none()
            knobs = _RuntimeKnobs.from_state(state)

            titles = await self._load_titles(session)
            matches = await self._load_matches(session)
            if not matches:
                await self._finalize(started, n_eval, n_buy, passes)
                return

            # Group matches by PM event — a single PM event may have multiple
            # external matches (Pinnacle + the_odds_api). We aggregate odds
            # across all of them per evaluation.
            matches_by_pm: dict[str, list[EventMatch]] = defaultdict(list)
            for m in matches:
                matches_by_pm[m.polymarket_event_id].append(m)

            # Pre-load token rows for matched events
            event_ids = set(matches_by_pm.keys())
            tokens_by_event: dict[str, list[PolymarketToken]] = defaultdict(list)
            tok_rows = (
                await session.execute(
                    select(PolymarketToken).where(
                        PolymarketToken.polymarket_event_id.in_(event_ids)
                    )
                )
            ).scalars().all()
            for t in tok_rows:
                tokens_by_event[t.polymarket_event_id].append(t)

            # Index external_events by id
            ext_ids = {m.external_event_id for m in matches}
            ext_rows = (
                await session.execute(
                    select(ExternalEvent).where(ExternalEvent.id.in_(ext_ids))
                )
            ).scalars().all()
            ext_by_id = {e.id: e for e in ext_rows}

            now = datetime.now(UTC)
            for pm_event_id, event_matches in matches_by_pm.items():
                title = titles.get(pm_event_id)
                # Pick the canonical external event for window/team-name use —
                # prefer Pinnacle when present (its start_time tends to be the
                # most reliable kickoff time), else the first match.
                canonical_ext = self._pick_canonical_ext(event_matches, ext_by_id)
                if canonical_ext is None:
                    continue
                start_time = _as_utc(canonical_ext.start_time)
                seconds_to_kickoff = (start_time - now).total_seconds()

                # Window gate (per PM event, not per token).
                if seconds_to_kickoff > knobs.max_time_to_game_s:
                    await self._log_one(
                        session,
                        action=Action.PASS_WINDOW_EARLY,
                        reason=f"kickoff in {seconds_to_kickoff/60:.0f}min (> {knobs.max_time_to_game_s/60:.0f}min)",
                        match=event_matches[0],
                        ext=canonical_ext,
                        title=title,
                        seconds_to_kickoff=seconds_to_kickoff,
                    )
                    passes[Action.PASS_WINDOW_EARLY] += 1
                    n_eval += 1
                    continue
                if seconds_to_kickoff < knobs.min_time_to_game_s:
                    await self._log_one(
                        session,
                        action=Action.PASS_WINDOW_LATE,
                        reason=f"kickoff in {seconds_to_kickoff/60:.0f}min (< {knobs.min_time_to_game_s/60:.0f}min) — live or imminent",
                        match=event_matches[0],
                        ext=canonical_ext,
                        title=title,
                        seconds_to_kickoff=seconds_to_kickoff,
                    )
                    passes[Action.PASS_WINDOW_LATE] += 1
                    n_eval += 1
                    continue

                # Devigged probs per book, aggregated across all matched ext_events.
                probs_by_book = await self._devigged_probs_by_book(
                    session, event_matches, ext_by_id, now
                )
                if not probs_by_book:
                    await self._log_one(
                        session,
                        action=Action.PASS_NO_EXT_SNAP,
                        reason="no recent external snapshot or devig failed across all sources",
                        match=event_matches[0],
                        ext=canonical_ext,
                        title=title,
                        seconds_to_kickoff=seconds_to_kickoff,
                    )
                    passes[Action.PASS_NO_EXT_SNAP] += 1
                    n_eval += 1
                    continue

                for token in tokens_by_event.get(pm_event_id, []):
                    n_eval += 1
                    action, reason, log_extra = await self._evaluate_token(
                        session,
                        token=token,
                        ext=canonical_ext,
                        probs_by_book=probs_by_book,
                        now=now,
                        knobs=knobs,
                    )
                    if action == Action.BUY:
                        n_buy += 1
                    else:
                        passes[action] += 1
                    await self._log_one(
                        session,
                        action=action,
                        reason=reason,
                        match=event_matches[0],
                        ext=canonical_ext,
                        title=title,
                        token=token,
                        seconds_to_kickoff=seconds_to_kickoff,
                        **log_extra,
                    )

            await session.commit()

        await self._finalize(started, n_eval, n_buy, passes)

    @staticmethod
    def _pick_canonical_ext(
        matches: list[EventMatch], ext_by_id: dict[int, ExternalEvent]
    ) -> ExternalEvent | None:
        # Pinnacle wins; otherwise first valid.
        pin_first = sorted(
            matches, key=lambda m: (0 if m.source == "pinnacle" else 1, m.id)
        )
        for m in pin_first:
            ext = ext_by_id.get(m.external_event_id)
            if ext is not None:
                return ext
        return None

    async def _finalize(
        self,
        started: float,
        n_eval: int,
        n_buy: int,
        passes: dict[str, int],
    ) -> None:
        self.stats.last_run_at = datetime.now(UTC)
        self.stats.last_run_duration_ms = (time.time() - started) * 1000.0
        self.stats.total_runs += 1
        self.stats.last_evaluations = n_eval
        self.stats.last_buys = n_buy
        self.stats.last_passes_by_reason = dict(passes)
        self.stats.total_buys += n_buy
        self.stats.total_passes += sum(passes.values())
        self.stats.total_decisions += n_eval
        logger.info(
            "DecisionEngine cycle: eval=%d buys=%d passes=%s in %.0fms",
            n_eval,
            n_buy,
            dict(passes),
            self.stats.last_run_duration_ms,
        )

    async def _load_titles(self, session: AsyncSession) -> dict[str, _PMEventTitle]:
        row = (
            await session.execute(select(PolymarketTreeCache).where(PolymarketTreeCache.id == 1))
        ).scalar_one_or_none()
        if row is None:
            return {}
        try:
            tree = json.loads(row.payload)
        except json.JSONDecodeError:
            return {}
        return _walk_tree_for_titles(tree)

    async def _load_matches(self, session: AsyncSession) -> list[EventMatch]:
        rows = (await session.execute(select(EventMatch))).scalars().all()
        return list(rows)

    async def _devigged_probs_by_book(
        self,
        session: AsyncSession,
        matches: list[EventMatch],
        ext_by_id: dict[int, ExternalEvent],
        now: datetime,
    ) -> dict[str, dict[str, float]]:
        """Aggregate snapshots across all matched external events for a single
        PM event, group by `snapshot.source` (the actual book key), then devig
        each book independently. Returns `{book_key: {side: prob}}`.

        Walks each match → external_event → snapshots-with-event_id-prefix.
        Different scrapers prefix differently:
          - PinnacleScraper       → event_id="pinnacle:{matchup_id}", source="pinnacle"
          - TheOddsApiScraper     → event_id="the_odds_api:{id}",     source="bet365"|...
        Both schemes are handled by querying by event_id only.
        """
        cutoff = now - timedelta(seconds=SNAPSHOT_MAX_AGE_S * 6)
        cutoff_naive = cutoff.replace(tzinfo=None)
        per_book_raw: dict[str, dict[str, float]] = defaultdict(dict)

        for match in matches:
            ext = ext_by_id.get(match.external_event_id)
            if ext is None:
                continue
            event_id_str = f"{ext.source}:{ext.source_event_id}"
            rows = (
                await session.execute(
                    select(OddsSnapshot)
                    .where(
                        and_(
                            OddsSnapshot.event_id == event_id_str,
                            OddsSnapshot.captured_at >= cutoff_naive,
                        )
                    )
                    .order_by(desc(OddsSnapshot.captured_at))
                    .limit(200)
                )
            ).scalars().all()
            # Keep the freshest per (book, outcome name).
            seen: set[tuple[str, str]] = set()
            for r in rows:
                if not r.source or not r.outcome:
                    continue
                key = (r.source, r.outcome)
                if key in seen:
                    continue
                seen.add(key)
                side = map_pm_outcome_to_side(r.outcome, ext.home_team, ext.away_team)
                if side is None:
                    continue
                prob = (
                    implied_prob_from_decimal(r.best_ask)
                    if r.best_ask is not None
                    else None
                )
                if prob is None:
                    continue
                # Don't overwrite a side already filled from an earlier match
                # — multiple ExternalEvents can refer to the same physical
                # game; the first one in we trust.
                per_book_raw[r.source].setdefault(side, prob)

        if not per_book_raw:
            return {}

        out: dict[str, dict[str, float]] = {}
        for book_key, raw in per_book_raw.items():
            devigged = devig_simple(raw)
            if devigged is None:
                continue
            out[book_key] = devigged.probs
        return out

    async def _evaluate_token(
        self,
        session: AsyncSession,
        *,
        token: PolymarketToken,
        ext: ExternalEvent,
        probs_by_book: dict[str, dict[str, float]],
        now: datetime,
        knobs: _RuntimeKnobs,
    ) -> tuple[str, str, dict[str, Any]]:
        side = map_pm_outcome_to_side(token.outcome, ext.home_team, ext.away_team)
        if side is None:
            return (
                Action.PASS_NO_MAP,
                f"can't map PM outcome {token.outcome!r} → home/away/draw",
                {"outcome_side": side},
            )
        # Latest poly snapshot for this token
        poly = (
            await session.execute(
                select(OddsSnapshot)
                .where(
                    and_(
                        OddsSnapshot.source == "polymarket",
                        OddsSnapshot.token_id == token.token_id,
                    )
                )
                .order_by(desc(OddsSnapshot.captured_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if poly is None or poly.best_ask is None:
            return (
                Action.PASS_NO_POLY_SNAP,
                "no recent polymarket snapshot or missing ask",
                {"outcome_side": side},
            )
        # Per-book probs for this side; build the weighted consensus.
        side_probs_per_book: dict[str, float] = {
            book: probs[side] for book, probs in probs_by_book.items() if side in probs
        }
        fair_prob = consensus_fair_prob(side_probs_per_book)
        if fair_prob is None:
            return (
                Action.PASS_DEVIG_FAILED,
                f"no devigged probs for {side} across {sorted(probs_by_book.keys())}",
                {"outcome_side": side},
            )
        if not (MIN_FAIR_PROB <= fair_prob <= MAX_FAIR_PROB):
            return (
                Action.PASS_FAIR_BOUNDS,
                f"fair prob {fair_prob:.3f} outside [{MIN_FAIR_PROB:.2f}, {MAX_FAIR_PROB:.2f}]",
                {
                    "outcome_side": side,
                    "fair_prob": fair_prob,
                    "poly_best_bid": poly.best_bid,
                    "poly_best_ask": poly.best_ask,
                    "poly_ask_depth_usd": poly.ask_depth_usd,
                    "pinnacle_raw_prob": side_probs_per_book.get("pinnacle"),
                },
            )
        depth = poly.ask_depth_usd or 0.0
        if depth < knobs.min_ask_depth_usd:
            return (
                Action.PASS_LIQUIDITY,
                f"ask depth ${depth:.0f} < ${knobs.min_ask_depth_usd:.0f}",
                {
                    "outcome_side": side,
                    "fair_prob": fair_prob,
                    "poly_best_bid": poly.best_bid,
                    "poly_best_ask": poly.best_ask,
                    "poly_ask_depth_usd": depth,
                    "pinnacle_raw_prob": side_probs_per_book.get("pinnacle"),
                },
            )
        ev = compute_ev_buy(fair_prob, poly.best_ask)
        book_summary = ",".join(sorted(side_probs_per_book.keys()))
        log_extra = {
            "outcome_side": side,
            "fair_prob": fair_prob,
            "poly_best_bid": poly.best_bid,
            "poly_best_ask": poly.best_ask,
            "poly_ask_depth_usd": depth,
            "pinnacle_raw_prob": side_probs_per_book.get("pinnacle"),
            "ev": ev,
            "proposed_price": poly.best_ask,
            "proposed_stake_usd": min(knobs.master_stake_usd, depth),
        }
        if ev < knobs.ev_threshold:
            return (
                Action.PASS_LOW_EV,
                f"EV {ev*100:.2f}% on {side} ({book_summary})",
                log_extra,
            )
        return (
            Action.BUY,
            f"EV {ev*100:.2f}% on {side} ({book_summary})",
            log_extra,
        )

    async def _log_one(
        self,
        session: AsyncSession,
        *,
        action: str,
        reason: str | None,
        match: EventMatch,
        ext: ExternalEvent,
        title: _PMEventTitle | None,
        token: PolymarketToken | None = None,
        seconds_to_kickoff: float | None = None,
        outcome_side: str | None = None,
        fair_prob: float | None = None,
        poly_best_bid: float | None = None,
        poly_best_ask: float | None = None,
        poly_ask_depth_usd: float | None = None,
        pinnacle_raw_prob: float | None = None,
        ev: float | None = None,
        proposed_price: float | None = None,
        proposed_stake_usd: float | None = None,
    ) -> None:
        session.add(
            DecisionLog(
                polymarket_event_id=match.polymarket_event_id,
                polymarket_token_id=token.token_id if token else None,
                pm_outcome=token.outcome if token else None,
                outcome_side=outcome_side,
                sport=title.sport if title else None,
                league=title.league if title else None,
                pm_event_title=title.title if title else None,
                action=action,
                reason=reason,
                fair_prob=fair_prob,
                poly_best_bid=poly_best_bid,
                poly_best_ask=poly_best_ask,
                poly_ask_depth_usd=poly_ask_depth_usd,
                pinnacle_decimal_odd=(1.0 / pinnacle_raw_prob) if pinnacle_raw_prob else None,
                pinnacle_raw_prob=pinnacle_raw_prob,
                ev=ev,
                proposed_stake_usd=proposed_stake_usd,
                proposed_price=proposed_price,
                seconds_to_kickoff=seconds_to_kickoff,
            )
        )
