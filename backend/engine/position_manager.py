"""PositionManager — sells open positions when one of three triggers fires.

Each cycle iterates every Position with status=OPEN and checks (in order):
  1. Stop-loss: best_bid dropped by `stop_loss_pct` from entry_price → cut.
  2. Time-critical: kickoff is closer than `min_time_to_game_minutes` → exit
     before live (we don't trade in-play).
  3. Convergence: best_bid caught up to the recomputed fair_price (within
     `exit_threshold`) → realize the spread.

Exits are LIMIT GTC at the current best_bid. If the bid is too thin, the
order rides until expiry / poller cancels it; the underlying Polymarket
position still resolves on the contract — we're not stuck.

The order poller in TradingEngine handles SUBMITTED → FILLED for SELLs and
closes the Position row with realized `pnl_usd`. Manager runs regardless of
`is_running` — exiting existing exposure is risk management, not a new trade.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.crypto.vault import VaultLocked, VaultState
from backend.engine.ev import (
    MAX_FAIR_PROB,
    MIN_FAIR_PROB,
    consensus_fair_prob,
    devig_simple,
    implied_prob_from_decimal,
    map_pm_outcome_to_side,
)
from backend.models import (
    BotState,
    EventMatch,
    ExternalEvent,
    OddsSnapshot,
    Order,
    Position,
)
from backend.polymarket.clob_client import (
    WalletNotConfigured,
    place_limit_order,
)

logger = logging.getLogger(__name__)

RUN_INTERVAL_S = 15.0
# Snapshots older than this aren't usable for fair-price calc. Larger than
# DecisionEngine's window because exit logic tolerates staler data — we'd
# rather close at a slightly old fair than hold blind.
SNAPSHOT_MAX_AGE_S = 9 * 60

# Order statuses still in flight; if an exit_order is in one of these we
# don't queue another one for the same position.
NON_TERMINAL_STATUSES = {"PENDING_SUBMIT", "SUBMITTED", "PARTIAL"}


class ExitReason:
    CONVERGENCE = "EXIT_CONVERGENCE"
    TIME_CRITICAL = "EXIT_TIME_CRITICAL"
    STOP_LOSS = "EXIT_STOP_LOSS"
    MANUAL = "EXIT_MANUAL"


@dataclass
class PositionManagerStats:
    last_run_at: datetime | None = None
    total_runs: int = 0
    last_open_positions: int = 0
    last_actions: dict[str, int] = field(default_factory=dict)
    total_sells_submitted: int = 0
    total_sells_failed: int = 0
    last_error: str | None = None
    vault_unlocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "total_runs": self.total_runs,
            "last_open_positions": self.last_open_positions,
            "last_actions": self.last_actions,
            "total_sells_submitted": self.total_sells_submitted,
            "total_sells_failed": self.total_sells_failed,
            "last_error": self.last_error,
            "vault_unlocked": self.vault_unlocked,
        }


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _bot_state(session: AsyncSession) -> BotState:
    row = (
        await session.execute(select(BotState).where(BotState.id == 1))
    ).scalar_one_or_none()
    if row is None:
        row = BotState(id=1)
        session.add(row)
        await session.commit()
    return row


async def _latest_poly_snapshot(
    session: AsyncSession, token_id: str
) -> OddsSnapshot | None:
    return (
        await session.execute(
            select(OddsSnapshot)
            .where(
                and_(
                    OddsSnapshot.source == "polymarket",
                    OddsSnapshot.token_id == token_id,
                )
            )
            .order_by(desc(OddsSnapshot.captured_at))
            .limit(1)
        )
    ).scalar_one_or_none()


def _pick_canonical_ext(
    matches: list[EventMatch], ext_by_id: dict[int, ExternalEvent]
) -> ExternalEvent | None:
    pin_first = sorted(
        matches, key=lambda m: (0 if m.source == "pinnacle" else 1, m.id)
    )
    for m in pin_first:
        ext = ext_by_id.get(m.external_event_id)
        if ext is not None:
            return ext
    return None


async def _compute_fair_for_side(
    session: AsyncSession,
    matches: list[EventMatch],
    ext_by_id: dict[int, ExternalEvent],
    now: datetime,
    target_side: str,
) -> float | None:
    """Mirror of DecisionEngine._devigged_probs_by_book but specialized to
    one side. Returns the consensus fair prob for `target_side` or None.
    """
    cutoff = now - timedelta(seconds=SNAPSHOT_MAX_AGE_S)
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
            per_book_raw[r.source].setdefault(side, prob)

    if not per_book_raw:
        return None
    side_probs_per_book: dict[str, float] = {}
    for book_key, raw in per_book_raw.items():
        devigged = devig_simple(raw)
        if devigged is None:
            continue
        if target_side in devigged.probs:
            side_probs_per_book[book_key] = devigged.probs[target_side]
    if not side_probs_per_book:
        return None
    return consensus_fair_prob(side_probs_per_book)


async def submit_exit_order(
    session: AsyncSession,
    position: Position,
    bid: float,
    reason_code: str,
    reason_msg: str,
) -> tuple[bool, str]:
    """Build + submit a SELL LIMIT GTC at `bid` for `position`.

    Returns (success, status_str). On success the Order row is SUBMITTED and
    `position.exit_order_id` points to it; the poller in TradingEngine will
    close out the Position when the order fills.

    Used by both the PositionManager loop and the manual close endpoint.
    """
    if not VaultState.is_unlocked():
        return False, "vault_locked"
    if bid <= 0 or bid >= 1.0:
        return False, "invalid_bid"
    size = round(position.size, 2)
    if size <= 0:
        return False, "invalid_size"

    order = Order(
        polymarket_order_id=None,
        polymarket_event_id=position.polymarket_event_id,
        token_id=position.token_id,
        outcome=position.outcome,
        side="SELL",
        price=bid,
        size=size,
        notional_usd=round(bid * size, 4),
        order_type="GTC",
        status="PENDING_SUBMIT",
        last_error=f"{reason_code}: {reason_msg}"[:512],
    )
    session.add(order)
    await session.flush()
    position.exit_order_id = order.id

    try:
        placed = await place_limit_order(
            session,
            token_id=position.token_id,
            price=bid,
            size=size,
            side="SELL",
            order_type="GTC",
        )
    except VaultLocked:
        order.status = "FAILED"
        order.last_error = "vault locked during submit"
        return False, "vault_locked"
    except WalletNotConfigured:
        order.status = "FAILED"
        order.last_error = "wallet not configured"
        return False, "wallet_not_configured"

    now = datetime.now(UTC)
    if placed.success:
        order.polymarket_order_id = placed.polymarket_order_id
        order.status = "SUBMITTED"
        order.submitted_at = now
        # Preserve the trigger reason on top of the submitted order's
        # error column? No — clear it so a real later error isn't masked.
        order.last_error = None
        logger.info(
            "EXIT SELL submitted: position=%s token=%s price=%.4f size=%.2f reason=%s",
            position.id,
            position.token_id,
            bid,
            size,
            reason_code,
        )
        return True, "submitted"
    order.status = "FAILED"
    order.last_error = (placed.error_msg or placed.status)[:512]
    logger.warning(
        "EXIT SELL failed: position=%s err=%s",
        position.id,
        placed.error_msg,
    )
    return False, "submit_failed"


class PositionManager:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.stats = PositionManagerStats()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("PositionManager starting (interval=%.0fs)", RUN_INTERVAL_S)
        while not self._stop.is_set():
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PositionManager cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RUN_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("PositionManager stopped")

    async def _cycle(self) -> None:
        async with self.session_factory() as session:
            state = await _bot_state(session)
            self.stats.vault_unlocked = VaultState.is_unlocked()

            positions = (
                await session.execute(
                    select(Position).where(Position.status == "OPEN")
                )
            ).scalars().all()
            self.stats.last_open_positions = len(positions)
            actions: dict[str, int] = defaultdict(int)

            now = datetime.now(UTC)
            for p in positions:
                # Skip if already exiting via a live order.
                if p.exit_order_id is not None:
                    existing = await session.get(Order, p.exit_order_id)
                    if existing and existing.status in NON_TERMINAL_STATUSES:
                        actions["already_exiting"] += 1
                        continue

                action = await self._evaluate_position(session, p, state, now)
                actions[action] += 1
                if action in {"exit_stop_loss", "exit_time_critical", "exit_convergence"}:
                    self.stats.total_sells_submitted += 1
                elif action == "submit_failed":
                    self.stats.total_sells_failed += 1

            self.stats.last_actions = dict(actions)
            await session.commit()

        self.stats.last_run_at = datetime.now(UTC)
        self.stats.total_runs += 1

    async def _evaluate_position(
        self,
        session: AsyncSession,
        position: Position,
        state: BotState,
        now: datetime,
    ) -> str:
        poly = await _latest_poly_snapshot(session, position.token_id)
        if poly is None or poly.best_bid is None or poly.best_bid <= 0:
            return "no_poly_snap"
        bid = float(poly.best_bid)

        # 1. Stop-loss has highest priority — fires even without fair-price data.
        sl_threshold = position.entry_price * (1.0 - state.stop_loss_pct)
        if bid <= sl_threshold:
            ok, _ = await submit_exit_order(
                session,
                position,
                bid,
                ExitReason.STOP_LOSS,
                f"bid {bid:.4f} <= entry {position.entry_price:.4f} * (1 - {state.stop_loss_pct:.2f})",
            )
            return "exit_stop_loss" if ok else "submit_failed"

        # 2. Need ExternalEvent for kickoff time + fair recalc.
        matches = (
            await session.execute(
                select(EventMatch).where(
                    EventMatch.polymarket_event_id == position.polymarket_event_id
                )
            )
        ).scalars().all()
        if not matches:
            return "no_match"
        ext_ids = {m.external_event_id for m in matches}
        ext_rows = (
            await session.execute(
                select(ExternalEvent).where(ExternalEvent.id.in_(ext_ids))
            )
        ).scalars().all()
        ext_by_id = {e.id: e for e in ext_rows}
        canonical = _pick_canonical_ext(list(matches), ext_by_id)
        if canonical is None:
            return "no_match"

        # 3. Time-critical: kickoff approaching, get out before live.
        kickoff = _as_utc(canonical.start_time)
        secs_to_kickoff = (kickoff - now).total_seconds()
        min_window_s = state.min_time_to_game_minutes * 60
        if secs_to_kickoff < min_window_s:
            ok, _ = await submit_exit_order(
                session,
                position,
                bid,
                ExitReason.TIME_CRITICAL,
                f"kickoff em {secs_to_kickoff/60:.1f}min (< {state.min_time_to_game_minutes}min)",
            )
            return "exit_time_critical" if ok else "submit_failed"

        # 4. Convergence: bid caught up to the fair price → realize the spread.
        side = map_pm_outcome_to_side(
            position.outcome or "", canonical.home_team, canonical.away_team
        )
        if side is None:
            return "no_map"
        fair_prob = await _compute_fair_for_side(
            session, list(matches), ext_by_id, now, side
        )
        if fair_prob is None:
            return "no_fair"
        if not (MIN_FAIR_PROB <= fair_prob <= MAX_FAIR_PROB):
            return "fair_bounds"

        # Sell when bid is within exit_threshold below fair (or above it).
        # i.e., the gap shrunk → market converged.
        target = fair_prob * (1.0 - state.exit_threshold)
        if bid >= target:
            ok, _ = await submit_exit_order(
                session,
                position,
                bid,
                ExitReason.CONVERGENCE,
                f"bid {bid:.4f} >= fair {fair_prob:.4f} * (1 - {state.exit_threshold:.3f}) = {target:.4f}",
            )
            return "exit_convergence" if ok else "submit_failed"
        return "hold"


async def manual_close_position(
    session: AsyncSession, position_id: int
) -> tuple[bool, str]:
    """Force-exit a single position at the current best bid.

    Used by `POST /api/positions/{id}/close`. Returns (success, message).
    """
    position = await session.get(Position, position_id)
    if position is None:
        return False, "position not found"
    if position.status != "OPEN":
        return False, f"position is {position.status}, not OPEN"
    if position.exit_order_id is not None:
        existing = await session.get(Order, position.exit_order_id)
        if existing and existing.status in NON_TERMINAL_STATUSES:
            return False, "exit order already in flight"

    poly = await _latest_poly_snapshot(session, position.token_id)
    if poly is None or poly.best_bid is None or poly.best_bid <= 0:
        return False, "no recent polymarket bid for token"
    bid = float(poly.best_bid)

    ok, status = await submit_exit_order(
        session,
        position,
        bid,
        ExitReason.MANUAL,
        f"manual close at bid {bid:.4f}",
    )
    if ok:
        await session.commit()
        return True, "exit submitted"
    await session.commit()
    return False, status
