"""RiskMonitor — periodic gate check independent of new trades.

The TradingEngine consults risk.check_risk before each BUY, but
realized-PnL drawdown can trip on a SELL fill too — a CLOSED position
adds to today's loss. So we also run enforce_risk every RUN_INTERVAL_S,
which mutates BotState to pause the bot if any *serious* limit is hit.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import BotState
from backend.polymarket.clob_client import get_usdc_balance
from backend.positions.risk import RiskReport, enforce_risk

logger = logging.getLogger(__name__)

RUN_INTERVAL_S = 60.0


@dataclass
class RiskMonitorStats:
    last_run_at: datetime | None = None
    total_runs: int = 0
    last_report: dict[str, Any] | None = None
    last_pause_reason: str | None = None
    auto_pauses_total: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "total_runs": self.total_runs,
            "last_report": self.last_report,
            "last_pause_reason": self.last_pause_reason,
            "auto_pauses_total": self.auto_pauses_total,
            "last_error": self.last_error,
        }


async def _bot_state(session: AsyncSession) -> BotState:
    row = (
        await session.execute(select(BotState).where(BotState.id == 1))
    ).scalar_one_or_none()
    if row is None:
        row = BotState(id=1)
        session.add(row)
        await session.commit()
    return row


class RiskMonitor:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.stats = RiskMonitorStats()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("RiskMonitor starting (interval=%.0fs)", RUN_INTERVAL_S)
        while not self._stop.is_set():
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("RiskMonitor cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RUN_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("RiskMonitor stopped")

    async def _cycle(self) -> None:
        async with self.session_factory() as session:
            state = await _bot_state(session)
            was_running = state.is_running
            # Best-effort bankroll read; None when vault is locked or call fails.
            try:
                bankroll = await get_usdc_balance(session)
            except Exception:
                logger.exception("get_usdc_balance failed in RiskMonitor")
                bankroll = None
            report = await enforce_risk(
                session,
                state,
                bankroll_usd=bankroll,
            )
            self.stats.last_report = report.to_dict()
            if was_running and not state.is_running:
                self.stats.auto_pauses_total += 1
                self.stats.last_pause_reason = state.last_pause_reason
            await session.commit()

        self.stats.last_run_at = datetime.now(UTC)
        self.stats.total_runs += 1
