"""Risk gate: hard limits the bot cannot violate.

Every BUY goes through `check_risk` before submission. A separate
`RiskMonitor` task runs `enforce_risk` periodically so daily-drawdown can
trip even with no new BUYs in flight (an OPEN position closes at a loss
and pushes today's realized PnL past the limit).

Checks (any failing trips the gate):
  - **concurrent**: open positions + non-terminal BUY orders < `max_concurrent_positions`
  - **drawdown**: sum of realized pnl_usd for positions CLOSED today (UTC)
                  ≥ -`max_daily_drawdown_usd`
  - **exposure**: gross notional of OPEN positions + non-terminal BUY orders
                  + intended new notional ≤ `max_total_exposure_usd`
  - **stake**: bankroll (live USDC) ≥ exposure + intended (when bankroll known)

Tripping the gate sets `BotState.is_running=False` plus
`last_pause_reason` / `last_paused_at`. Re-running requires the user to
manually flip the switch back on at `/config/risk` — the bot does not
self-unpause.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import BotState, Order, Position

logger = logging.getLogger(__name__)

# Order statuses that still tie up funds (await fill or cancel).
NON_TERMINAL_BUY_STATUSES = ("PENDING_SUBMIT", "SUBMITTED", "PARTIAL")


@dataclass(slots=True)
class RiskViolation:
    code: str
    current: float
    limit: float
    message: str


@dataclass(slots=True)
class RiskReport:
    passed: bool
    violations: list[RiskViolation] = field(default_factory=list)
    open_positions: int = 0
    pending_orders: int = 0
    concurrent_count: int = 0
    open_exposure_usd: float = 0.0
    realized_pnl_today_usd: float = 0.0
    bankroll_usd: float | None = None
    intended_notional_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": [
                {
                    "code": v.code,
                    "current": v.current,
                    "limit": v.limit,
                    "message": v.message,
                }
                for v in self.violations
            ],
            "open_positions": self.open_positions,
            "pending_orders": self.pending_orders,
            "concurrent_count": self.concurrent_count,
            "open_exposure_usd": self.open_exposure_usd,
            "realized_pnl_today_usd": self.realized_pnl_today_usd,
            "bankroll_usd": self.bankroll_usd,
            "intended_notional_usd": self.intended_notional_usd,
        }


def _start_of_today_utc() -> datetime:
    return datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)


async def _open_positions_notional(session: AsyncSession) -> tuple[int, float]:
    rows = (
        await session.execute(
            select(Position).where(Position.status == "OPEN")
        )
    ).scalars().all()
    notional = sum(p.size * p.entry_price for p in rows)
    return len(rows), float(notional)


async def _pending_buy_orders(session: AsyncSession) -> tuple[int, float]:
    rows = (
        await session.execute(
            select(Order).where(
                and_(
                    Order.side == "BUY",
                    Order.status.in_(NON_TERMINAL_BUY_STATUSES),
                )
            )
        )
    ).scalars().all()
    notional = sum(o.notional_usd for o in rows)
    return len(rows), float(notional)


async def _realized_pnl_today(session: AsyncSession) -> float:
    """Sum of pnl_usd for positions that CLOSED in the current UTC day."""
    start_naive = _start_of_today_utc().replace(tzinfo=None)
    total = (
        await session.execute(
            select(func.coalesce(func.sum(Position.pnl_usd), 0.0)).where(
                and_(
                    Position.status == "CLOSED",
                    Position.exit_at >= start_naive,
                )
            )
        )
    ).scalar_one()
    return float(total or 0.0)


async def check_risk(
    session: AsyncSession,
    state: BotState,
    *,
    intended_notional_usd: float = 0.0,
    bankroll_usd: float | None = None,
) -> RiskReport:
    """Pure read — never mutates BotState. Returns RiskReport."""
    open_n, open_notional = await _open_positions_notional(session)
    pend_n, pend_notional = await _pending_buy_orders(session)
    realized = await _realized_pnl_today(session)
    exposure_now = open_notional + pend_notional
    concurrent = open_n + pend_n

    violations: list[RiskViolation] = []

    if concurrent + (1 if intended_notional_usd > 0 else 0) > state.max_concurrent_positions:
        violations.append(
            RiskViolation(
                code="CONCURRENT",
                current=float(concurrent),
                limit=float(state.max_concurrent_positions),
                message=f"posições + ordens pendentes ({concurrent}) ≥ limite ({state.max_concurrent_positions})",
            )
        )

    if realized <= -state.max_daily_drawdown_usd:
        violations.append(
            RiskViolation(
                code="DRAWDOWN",
                current=realized,
                limit=-state.max_daily_drawdown_usd,
                message=f"prejuízo do dia ${realized:.2f} ≤ limite -${state.max_daily_drawdown_usd:.2f}",
            )
        )

    projected_exposure = exposure_now + max(0.0, intended_notional_usd)
    if projected_exposure > state.max_total_exposure_usd:
        violations.append(
            RiskViolation(
                code="EXPOSURE",
                current=projected_exposure,
                limit=state.max_total_exposure_usd,
                message=f"exposição projetada ${projected_exposure:.2f} > limite ${state.max_total_exposure_usd:.2f}",
            )
        )

    if bankroll_usd is not None and projected_exposure > bankroll_usd:
        violations.append(
            RiskViolation(
                code="STAKE",
                current=projected_exposure,
                limit=bankroll_usd,
                message=f"exposição ${projected_exposure:.2f} > saldo USDC ${bankroll_usd:.2f}",
            )
        )

    return RiskReport(
        passed=not violations,
        violations=violations,
        open_positions=open_n,
        pending_orders=pend_n,
        concurrent_count=concurrent,
        open_exposure_usd=exposure_now,
        realized_pnl_today_usd=realized,
        bankroll_usd=bankroll_usd,
        intended_notional_usd=intended_notional_usd,
    )


async def enforce_risk(
    session: AsyncSession,
    state: BotState,
    *,
    intended_notional_usd: float = 0.0,
    bankroll_usd: float | None = None,
) -> RiskReport:
    """Check + auto-pause on violation.

    DRAWDOWN, EXPOSURE and STAKE violations are always pause-worthy.
    CONCURRENT is a soft gate when there's no intended order — it only
    means "no room right now", not "broken state" — so we don't pause for
    it unless it tripped *with* an intended order.
    """
    report = await check_risk(
        session,
        state,
        intended_notional_usd=intended_notional_usd,
        bankroll_usd=bankroll_usd,
    )
    if report.passed:
        return report

    serious_codes = {"DRAWDOWN", "EXPOSURE", "STAKE"}
    if intended_notional_usd > 0:
        serious_codes.add("CONCURRENT")
    serious = [v for v in report.violations if v.code in serious_codes]
    if not serious:
        return report

    if state.is_running:
        msg = "; ".join(v.message for v in serious)[:512]
        state.is_running = False
        state.last_pause_reason = msg
        state.last_paused_at = datetime.now(UTC)
        logger.warning("Bot auto-paused by risk: %s", msg)
    return report
