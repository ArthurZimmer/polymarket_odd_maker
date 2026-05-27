"""TradingEngine — turns DecisionLog BUYs into real Polymarket orders.

Run as a long-lived asyncio task. Each cycle:
  1. Read BotState; if `is_running` False or vault locked, no-op.
  2. Pick BUY decisions newer than the last cursor we acted on.
  3. For each: dedupe against the current `orders` table (skip if there's
     already a non-terminal order for the same token), build an Order row in
     PENDING_SUBMIT, then call `place_limit_order`.
  4. On ACK: update with order id + SUBMITTED.
  5. On failure: mark FAILED + record error.

The poller for SUBMITTED → FILLED/CANCELLED runs concurrently — it polls the
CLOB for live orders, updates fill state, and inserts Position rows on full
or partial fills.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.crypto.vault import VaultLocked, VaultState
from backend.models import BotState, DecisionLog, Order, Position, PolymarketToken
from backend.polymarket.clob_client import (
    WalletNotConfigured,
    cancel_order,
    get_order,
    get_usdc_balance,
    place_limit_order,
)
from backend.positions.risk import enforce_risk

logger = logging.getLogger(__name__)

# Reasonable defaults — overridable via BotState.
TRADING_INTERVAL_S = 5.0
ORDER_POLL_INTERVAL_S = 3.0
ORDER_STALE_TIMEOUT_S = 600.0          # cancel SUBMITTED orders that don't fill in 10min
ORDER_RECONCILE_ON_START_S = 30.0      # cancel orders left over from previous run older than this

# Terminal states we never poll again.
TERMINAL_STATUSES = {"FILLED", "CANCELLED", "FAILED"}


@dataclass
class TradingStats:
    last_run_at: datetime | None = None
    total_runs: int = 0
    total_orders_attempted: int = 0
    total_orders_submitted: int = 0
    total_orders_failed: int = 0
    total_fills: int = 0
    last_error: str | None = None
    last_action_summary: dict[str, int] = field(default_factory=dict)
    bot_running: bool = False
    vault_unlocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "total_runs": self.total_runs,
            "total_orders_attempted": self.total_orders_attempted,
            "total_orders_submitted": self.total_orders_submitted,
            "total_orders_failed": self.total_orders_failed,
            "total_fills": self.total_fills,
            "last_error": self.last_error,
            "last_action_summary": self.last_action_summary,
            "bot_running": self.bot_running,
            "vault_unlocked": self.vault_unlocked,
        }


class TradingEngine:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.stats = TradingStats()
        self._stop = asyncio.Event()
        self._last_decision_id: int = 0

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("TradingEngine starting (interval=%.1fs)", TRADING_INTERVAL_S)
        await self._reconcile_orphans_on_start()
        # Kick off the order poller concurrently.
        poller = asyncio.create_task(self._order_poller(), name="trading-poller")
        try:
            while not self._stop.is_set():
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("TradingEngine cycle failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=TRADING_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
        finally:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
            await self._cancel_all_live_orders("graceful shutdown")
        logger.info("TradingEngine stopped")

    async def _bot_state(self, session: AsyncSession) -> BotState:
        row = (
            await session.execute(select(BotState).where(BotState.id == 1))
        ).scalar_one_or_none()
        if row is None:
            row = BotState(id=1)
            session.add(row)
            await session.commit()
        return row

    async def _cycle(self) -> None:
        async with self.session_factory() as session:
            state = await self._bot_state(session)
            self.stats.bot_running = state.is_running
            self.stats.vault_unlocked = VaultState.is_unlocked()
            if not state.is_running:
                self.stats.last_action_summary = {"skipped": 1, "reason": 0}
                await self._tick()
                return
            if not VaultState.is_unlocked():
                self.stats.last_error = "vault locked — cannot sign orders"
                await self._tick()
                return

            actions: dict[str, int] = {
                "placed": 0,
                "skipped_dup": 0,
                "skipped_concurrency": 0,
                "skipped_risk": 0,
                "failed": 0,
            }

            # Concurrency gate: count current non-terminal orders.
            open_orders = (
                await session.execute(
                    select(Order).where(Order.status.notin_(TERMINAL_STATUSES))
                )
            ).scalars().all()
            open_orders_by_token = {o.token_id: o for o in open_orders}
            open_positions = (
                await session.execute(
                    select(Position).where(Position.status == "OPEN")
                )
            ).scalars().all()
            concurrent_count = len(open_orders) + len(open_positions)

            # Pick recent BUYs we haven't processed.
            buys = (
                await session.execute(
                    select(DecisionLog)
                    .where(
                        and_(
                            DecisionLog.action == "BUY",
                            DecisionLog.id > self._last_decision_id,
                        )
                    )
                    .order_by(DecisionLog.id.asc())
                    .limit(20)
                )
            ).scalars().all()

            # Read bankroll once per cycle — cached for 30s upstream.
            bankroll = await get_usdc_balance(session)

            for d in buys:
                self._last_decision_id = d.id
                if d.polymarket_token_id is None or d.proposed_price is None:
                    continue
                if d.polymarket_token_id in open_orders_by_token:
                    actions["skipped_dup"] += 1
                    continue
                if any(p.token_id == d.polymarket_token_id for p in open_positions):
                    actions["skipped_dup"] += 1
                    continue
                if concurrent_count >= state.max_concurrent_positions:
                    actions["skipped_concurrency"] += 1
                    break  # don't keep scanning; we're at the cap

                # Pre-trade risk gate. enforce_risk auto-pauses the bot on
                # any *serious* violation; if it did, we break out of the
                # loop since further BUYs would be rejected too.
                stake_usd = min(
                    state.master_stake_usd,
                    float(d.poly_ask_depth_usd or state.master_stake_usd),
                )
                report = await enforce_risk(
                    session,
                    state,
                    intended_notional_usd=stake_usd,
                    bankroll_usd=bankroll,
                )
                if not report.passed:
                    actions["skipped_risk"] += 1
                    self.stats.last_error = (
                        "risk: " + "; ".join(v.message for v in report.violations)
                    )[:512]
                    if not state.is_running:
                        # enforce_risk paused us — stop processing.
                        break
                    continue

                placed = await self._place_for_decision(session, d, state)
                if placed:
                    actions["placed"] += 1
                    self.stats.total_orders_submitted += 1
                    concurrent_count += 1
                else:
                    actions["failed"] += 1
                    self.stats.total_orders_failed += 1
                self.stats.total_orders_attempted += 1

            self.stats.last_action_summary = actions
            await session.commit()
        await self._tick()

    async def _tick(self) -> None:
        self.stats.last_run_at = datetime.now(UTC)
        self.stats.total_runs += 1

    async def _place_for_decision(
        self, session: AsyncSession, d: DecisionLog, state: BotState
    ) -> bool:
        """Build & submit an order for one BUY decision.

        Stake comes from BotState.master_stake_usd, clamped to poly_ask_depth.
        Size (shares) = stake / price.
        """
        price = float(d.proposed_price)  # type: ignore[arg-type]
        depth = float(d.poly_ask_depth_usd or 0.0)
        stake_usd = min(state.master_stake_usd, depth) if depth > 0 else state.master_stake_usd
        if stake_usd <= 0 or price <= 0 or price >= 1.0:
            logger.warning(
                "Skipping BUY id=%s: invalid stake/price (stake=%s price=%s)",
                d.id,
                stake_usd,
                price,
            )
            return False
        # Polymarket size unit is shares. Round down to 2 decimals to satisfy
        # CLOB tick size and avoid floating dust.
        size = round(stake_usd / price, 2)
        if size <= 0:
            return False

        # Persist Order row in PENDING_SUBMIT first so we have an audit trail
        # even if the post fails (or the process dies before ACK).
        order = Order(
            polymarket_order_id=None,
            polymarket_event_id=d.polymarket_event_id,
            token_id=d.polymarket_token_id,  # type: ignore[arg-type]
            outcome=d.pm_outcome,
            side="BUY",
            price=price,
            size=size,
            notional_usd=round(price * size, 4),
            order_type="GTC",
            status="PENDING_SUBMIT",
            decision_id=d.id,
        )
        session.add(order)
        await session.flush()  # populate order.id

        # Submit. The CLOB call is long-running enough that we don't want to
        # hold any transactional locks while waiting — flush, then submit,
        # then update.
        try:
            placed = await place_limit_order(
                session,
                token_id=order.token_id,
                price=price,
                size=size,
                side="BUY",
                order_type="GTC",
            )
        except VaultLocked:
            order.status = "FAILED"
            order.last_error = "vault locked during submit"
            self.stats.last_error = order.last_error
            return False
        except WalletNotConfigured:
            order.status = "FAILED"
            order.last_error = "wallet not configured"
            self.stats.last_error = order.last_error
            return False

        now = datetime.now(UTC)
        if placed.success:
            order.polymarket_order_id = placed.polymarket_order_id
            order.status = "SUBMITTED"
            order.submitted_at = now
            logger.info(
                "BUY submitted: token=%s price=%.4f size=%.2f stake=$%.2f pm_id=%s",
                order.token_id,
                price,
                size,
                stake_usd,
                placed.polymarket_order_id,
            )
            return True
        order.status = "FAILED"
        order.last_error = (placed.error_msg or placed.status)[:512]
        self.stats.last_error = order.last_error
        logger.warning(
            "BUY post failed: token=%s err=%s",
            order.token_id,
            placed.error_msg,
        )
        return False

    # ── Order poller ──────────────────────────────────────────────────

    async def _order_poller(self) -> None:
        """Periodically refresh SUBMITTED orders. Promote to FILLED/CANCELLED
        and create Position rows when filled."""
        while not self._stop.is_set():
            try:
                await self._poll_open_orders()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("order poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=ORDER_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def _poll_open_orders(self) -> None:
        if not VaultState.is_unlocked():
            return
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Order)
                    .where(
                        and_(
                            Order.status == "SUBMITTED",
                            Order.polymarket_order_id.is_not(None),
                        )
                    )
                    .order_by(Order.submitted_at.asc())
                    .limit(20)
                )
            ).scalars().all()
            now = datetime.now(UTC)
            for o in rows:
                resp = await get_order(session, o.polymarket_order_id)  # type: ignore[arg-type]
                o.last_polled_at = now
                if not resp:
                    # Stale check: cancel if outstanding too long.
                    age = (
                        now - (o.submitted_at.replace(tzinfo=UTC) if o.submitted_at else now)
                    ).total_seconds()
                    if age > ORDER_STALE_TIMEOUT_S:
                        await self._mark_cancelled(session, o, reason="stale, no remote echo")
                    continue
                status = (resp.get("status") or "").lower()
                size_matched = float(resp.get("size_matched") or 0.0)
                if status in {"matched", "filled"}:
                    await self._mark_filled(session, o, filled_size=size_matched or o.size, avg_price=o.price)
                elif status in {"canceled", "cancelled"}:
                    await self._mark_cancelled(session, o, reason="remote canceled")
                else:
                    # Still live. Partial fill tracking:
                    if size_matched and size_matched != o.filled_size:
                        o.filled_size = size_matched
                    age = (
                        now - (o.submitted_at.replace(tzinfo=UTC) if o.submitted_at else now)
                    ).total_seconds()
                    if age > ORDER_STALE_TIMEOUT_S:
                        ok = await cancel_order(session, o.polymarket_order_id)  # type: ignore[arg-type]
                        if ok:
                            await self._mark_cancelled(session, o, reason=f"stale > {ORDER_STALE_TIMEOUT_S:.0f}s")
            await session.commit()

    async def _mark_filled(
        self, session: AsyncSession, order: Order, *, filled_size: float, avg_price: float
    ) -> None:
        order.status = "FILLED"
        order.filled_size = filled_size
        order.filled_avg_price = avg_price
        order.filled_at = datetime.now(UTC)
        self.stats.total_fills += 1
        if order.side == "BUY":
            session.add(
                Position(
                    polymarket_event_id=order.polymarket_event_id,
                    token_id=order.token_id,
                    outcome=order.outcome,
                    size=filled_size,
                    entry_price=avg_price,
                    entry_order_id=order.id,
                    status="OPEN",
                )
            )
            logger.info(
                "BUY FILLED: token=%s size=%.2f @ %.4f → OPEN position",
                order.token_id,
                filled_size,
                avg_price,
            )
            return

        # SELL → close the Position that submitted this exit order.
        position = (
            await session.execute(
                select(Position).where(Position.exit_order_id == order.id)
            )
        ).scalar_one_or_none()
        if position is None:
            # Manual sale outside of the manager flow — log and bail, the
            # order is still recorded. Could happen if exit_order_id linkage
            # was lost (db restored, manual SQL, etc.).
            logger.warning(
                "SELL FILLED with no matching Position: order=%s token=%s",
                order.id,
                order.token_id,
            )
            return
        position.status = "CLOSED"
        position.exit_price = avg_price
        position.exit_at = order.filled_at
        position.pnl_usd = round(
            (avg_price - position.entry_price) * filled_size, 4
        )
        logger.info(
            "SELL FILLED: position=%s token=%s @ %.4f → CLOSED pnl=$%.2f",
            position.id,
            position.token_id,
            avg_price,
            position.pnl_usd,
        )

    async def _mark_cancelled(
        self, session: AsyncSession, order: Order, *, reason: str
    ) -> None:
        order.status = "CANCELLED"
        order.cancelled_at = datetime.now(UTC)
        order.last_error = reason
        logger.info("ORDER CANCELLED: id=%s reason=%s", order.polymarket_order_id, reason)

    # ── Startup / shutdown helpers ────────────────────────────────────

    async def _reconcile_orphans_on_start(self) -> None:
        """Mark anything still PENDING_SUBMIT from a previous run as FAILED.

        SUBMITTED orders from a prior run are left to the poller — it will
        either confirm or cancel via the CLOB. PENDING_SUBMIT means we never
        even got an ack, so it's safe to drop.
        """
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Order).where(Order.status == "PENDING_SUBMIT")
                )
            ).scalars().all()
            if not rows:
                return
            now = datetime.now(UTC)
            for r in rows:
                r.status = "FAILED"
                r.last_error = "abandoned by previous process"
                r.cancelled_at = now
            await session.commit()
            logger.info(
                "Reconciled %d orphan PENDING_SUBMIT orders from previous run",
                len(rows),
            )

    async def _cancel_all_live_orders(self, reason: str) -> None:
        """Best-effort cancel of all SUBMITTED orders on shutdown."""
        if not VaultState.is_unlocked():
            return
        try:
            async with self.session_factory() as session:
                rows = (
                    await session.execute(
                        select(Order).where(
                            and_(
                                Order.status == "SUBMITTED",
                                Order.polymarket_order_id.is_not(None),
                            )
                        )
                    )
                ).scalars().all()
                for o in rows:
                    ok = await cancel_order(session, o.polymarket_order_id)  # type: ignore[arg-type]
                    if ok:
                        await self._mark_cancelled(session, o, reason=reason)
                await session.commit()
                if rows:
                    logger.info("Cancelled %d live orders on shutdown", len(rows))
        except Exception:
            logger.exception("cancel_all_live_orders failed")
