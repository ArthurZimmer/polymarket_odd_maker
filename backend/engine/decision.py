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
    DecisionLog,
    EventMatch,
    ExternalEvent,
    OddsSnapshot,
    PolymarketToken,
    PolymarketTreeCache,
)

logger = logging.getLogger(__name__)


# Defaults from the plan — will become user-tunable in Etapa 11 (Risk).
EV_THRESHOLD = 0.03                # 3% EV required to fire BUY
EXIT_THRESHOLD = 0.005             # used by PositionManager later
MIN_TIME_TO_GAME_S = 5 * 60        # 5 min — too close to live: skip
MAX_TIME_TO_GAME_S = 120 * 60      # 2h — too far out: orderbooks too thin
MIN_ASK_DEPTH_USD = 100.0          # need at least this in book at best ask
SNAPSHOT_MAX_AGE_S = 90.0          # poly/ext snapshot can't be older than this
MASTER_STAKE_USD = 10.0            # used as base stake for proposed_stake_usd
RUN_INTERVAL_S = 10.0              # how often the engine cycles


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
            "DecisionEngine starting (dry_run=%s, interval=%.0fs, ev>=%.0f%%)",
            self.dry_run,
            RUN_INTERVAL_S,
            EV_THRESHOLD * 100,
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
            titles = await self._load_titles(session)
            matches = await self._load_matches(session)
            if not matches:
                await self._finalize(started, n_eval, n_buy, passes)
                return

            # Pre-load token rows for matched events
            event_ids = {m.polymarket_event_id for m in matches}
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

            # Index external_events by id (for home/away/start_time lookups)
            ext_ids = {m.external_event_id for m in matches}
            ext_rows = (
                await session.execute(
                    select(ExternalEvent).where(ExternalEvent.id.in_(ext_ids))
                )
            ).scalars().all()
            ext_by_id = {e.id: e for e in ext_rows}

            now = datetime.now(UTC)
            for match in matches:
                ext = ext_by_id.get(match.external_event_id)
                if ext is None:
                    continue
                title = titles.get(match.polymarket_event_id)
                start_time = _as_utc(ext.start_time)
                seconds_to_kickoff = (start_time - now).total_seconds()

                # Window gate (applied per-event, not per-token, so we don't
                # waste cycles evaluating tokens for events out of scope).
                if seconds_to_kickoff > MAX_TIME_TO_GAME_S:
                    await self._log_one(
                        session,
                        action=Action.PASS_WINDOW_EARLY,
                        reason=f"kickoff in {seconds_to_kickoff/60:.0f}min (> {MAX_TIME_TO_GAME_S/60:.0f}min)",
                        match=match,
                        ext=ext,
                        title=title,
                        seconds_to_kickoff=seconds_to_kickoff,
                    )
                    passes[Action.PASS_WINDOW_EARLY] += 1
                    n_eval += 1
                    continue
                if seconds_to_kickoff < MIN_TIME_TO_GAME_S:
                    await self._log_one(
                        session,
                        action=Action.PASS_WINDOW_LATE,
                        reason=f"kickoff in {seconds_to_kickoff/60:.0f}min (< {MIN_TIME_TO_GAME_S/60:.0f}min) — live or imminent",
                        match=match,
                        ext=ext,
                        title=title,
                        seconds_to_kickoff=seconds_to_kickoff,
                    )
                    passes[Action.PASS_WINDOW_LATE] += 1
                    n_eval += 1
                    continue

                # Cache Pinnacle devig once per event
                pin_probs_devigged = await self._latest_devigged_pinnacle(
                    session, ext, now
                )
                if pin_probs_devigged is None:
                    await self._log_one(
                        session,
                        action=Action.PASS_NO_EXT_SNAP,
                        reason="no recent pinnacle snapshot or devig failed",
                        match=match,
                        ext=ext,
                        title=title,
                        seconds_to_kickoff=seconds_to_kickoff,
                    )
                    passes[Action.PASS_NO_EXT_SNAP] += 1
                    n_eval += 1
                    continue

                for token in tokens_by_event.get(match.polymarket_event_id, []):
                    n_eval += 1
                    action, reason, log_extra = await self._evaluate_token(
                        session,
                        token=token,
                        ext=ext,
                        pin_probs=pin_probs_devigged,
                        now=now,
                    )
                    if action == Action.BUY:
                        n_buy += 1
                    else:
                        passes[action] += 1
                    await self._log_one(
                        session,
                        action=action,
                        reason=reason,
                        match=match,
                        ext=ext,
                        title=title,
                        token=token,
                        seconds_to_kickoff=seconds_to_kickoff,
                        **log_extra,
                    )

            await session.commit()

        await self._finalize(started, n_eval, n_buy, passes)

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

    async def _latest_devigged_pinnacle(
        self, session: AsyncSession, ext: ExternalEvent, now: datetime
    ) -> dict[str, float] | None:
        """Pull the latest Pinnacle snapshot per outcome side for this event
        and devig the implied probs. Returns {'home':p, 'away':p, 'draw'?:p}.
        """
        cutoff = now - timedelta(seconds=SNAPSHOT_MAX_AGE_S * 6)  # be a bit forgiving
        ev_id_str = f"pinnacle:{ext.source_event_id}"
        rows = (
            await session.execute(
                select(OddsSnapshot)
                .where(
                    and_(
                        OddsSnapshot.source == "pinnacle",
                        OddsSnapshot.event_id == ev_id_str,
                        OddsSnapshot.captured_at >= cutoff.replace(tzinfo=None),
                    )
                )
                .order_by(desc(OddsSnapshot.captured_at))
                .limit(50)
            )
        ).scalars().all()
        if not rows:
            return None
        # First occurrence per outcome name is the freshest.
        latest_by_outcome: dict[str, OddsSnapshot] = {}
        for r in rows:
            if r.outcome and r.outcome not in latest_by_outcome:
                latest_by_outcome[r.outcome] = r
        # Map outcome name → side via the external_event's teams.
        from backend.engine.ev import map_pm_outcome_to_side  # local to avoid cycles

        raw_probs: dict[str, float] = {}
        for outcome_name, snap in latest_by_outcome.items():
            side = map_pm_outcome_to_side(outcome_name, ext.home_team, ext.away_team)
            if side is None:
                # Pinnacle outcome name is the raw team name; if mapping fails
                # the team probably doesn't match the canonical home/away (rare).
                continue
            decimal_odd = snap.best_ask
            prob = implied_prob_from_decimal(decimal_odd) if decimal_odd else None
            if prob is None:
                continue
            raw_probs[side] = prob
        if not raw_probs:
            return None
        devigged = devig_simple(raw_probs)
        if devigged is None:
            return None
        return devigged.probs

    async def _evaluate_token(
        self,
        session: AsyncSession,
        *,
        token: PolymarketToken,
        ext: ExternalEvent,
        pin_probs: dict[str, float],
        now: datetime,
    ) -> tuple[str, str, dict[str, Any]]:
        # Outcome side
        side = map_pm_outcome_to_side(token.outcome, ext.home_team, ext.away_team)
        if side is None or side not in pin_probs:
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
        # Sanity bounds on fair prob
        fair_prob = consensus_fair_prob({"pinnacle": pin_probs[side]})
        if fair_prob is None:
            return (
                Action.PASS_DEVIG_FAILED,
                "consensus fair prob unavailable",
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
                    "pinnacle_raw_prob": pin_probs[side],
                },
            )
        # Liquidity
        depth = poly.ask_depth_usd or 0.0
        if depth < MIN_ASK_DEPTH_USD:
            return (
                Action.PASS_LIQUIDITY,
                f"ask depth ${depth:.0f} < ${MIN_ASK_DEPTH_USD:.0f}",
                {
                    "outcome_side": side,
                    "fair_prob": fair_prob,
                    "poly_best_bid": poly.best_bid,
                    "poly_best_ask": poly.best_ask,
                    "poly_ask_depth_usd": depth,
                    "pinnacle_raw_prob": pin_probs[side],
                },
            )
        # EV
        ev = compute_ev_buy(fair_prob, poly.best_ask)
        log_extra = {
            "outcome_side": side,
            "fair_prob": fair_prob,
            "poly_best_bid": poly.best_bid,
            "poly_best_ask": poly.best_ask,
            "poly_ask_depth_usd": depth,
            "pinnacle_raw_prob": pin_probs[side],
            "ev": ev,
            "proposed_price": poly.best_ask,
            "proposed_stake_usd": min(MASTER_STAKE_USD, depth),
        }
        if ev < EV_THRESHOLD:
            return (
                Action.PASS_LOW_EV,
                f"EV {ev*100:.2f}% < {EV_THRESHOLD*100:.0f}%",
                log_extra,
            )
        return (Action.BUY, f"EV {ev*100:.2f}% on {side}", log_extra)

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
