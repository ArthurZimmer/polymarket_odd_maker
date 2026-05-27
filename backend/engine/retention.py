"""Periodic database retention — keeps the SQLite DB from growing without bound.

The watcher produces ~600 msgs/min (now deduped on bid/ask change) and the
DecisionEngine ~50 rows/min (after dropping noisy passes). Without a TTL
job the file gets to ~17GB inside a couple weeks. This task deletes the
debris on a configurable cadence so the trading-critical tables (orders,
positions, decision_log BUYs) stay around but the firehose history doesn't.

Defaults are tuned for a single-user dev box:
  - odds_snapshots:        keep last 2h  (used by EV math; older = irrelevant)
  - decision_log non-BUY:  keep last 7d  (debug audit; older is noise)
  - decision_log BUYs:     never purge   (links to Orders/Positions)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete

from backend.models import DecisionLog, OddsSnapshot

logger = logging.getLogger(__name__)

RUN_INTERVAL_S = 3600.0  # once per hour
ODDS_SNAPSHOT_TTL_H = 2
DECISION_LOG_NON_BUY_TTL_D = 7


@dataclass
class RetentionStats:
    last_run_at: datetime | None = None
    total_runs: int = 0
    last_snapshots_deleted: int = 0
    last_decisions_deleted: int = 0
    total_snapshots_deleted: int = 0
    total_decisions_deleted: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "total_runs": self.total_runs,
            "last_snapshots_deleted": self.last_snapshots_deleted,
            "last_decisions_deleted": self.last_decisions_deleted,
            "total_snapshots_deleted": self.total_snapshots_deleted,
            "total_decisions_deleted": self.total_decisions_deleted,
            "last_error": self.last_error,
        }


class RetentionMonitor:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.stats = RetentionStats()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info(
            "RetentionMonitor starting (interval=%.0fh, snapshots TTL=%dh, "
            "decision-log non-BUY TTL=%dd)",
            RUN_INTERVAL_S / 3600,
            ODDS_SNAPSHOT_TTL_H,
            DECISION_LOG_NON_BUY_TTL_D,
        )
        # Run once shortly after startup so we don't wait a full hour the
        # first time. Then settle into RUN_INTERVAL_S cadence.
        first_wait = min(60.0, RUN_INTERVAL_S)
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=first_wait)
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("RetentionMonitor cycle failed")
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RUN_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("RetentionMonitor stopped")

    async def _cycle(self) -> None:
        now = datetime.now(UTC)
        snap_cutoff = (now - timedelta(hours=ODDS_SNAPSHOT_TTL_H)).replace(tzinfo=None)
        decision_cutoff = (now - timedelta(days=DECISION_LOG_NON_BUY_TTL_D)).replace(tzinfo=None)

        async with self.session_factory() as session:
            snaps_deleted = (
                await session.execute(
                    delete(OddsSnapshot).where(OddsSnapshot.captured_at < snap_cutoff)
                )
            ).rowcount or 0
            # Keep BUY rows forever (they back the trading history / PnL math).
            decisions_deleted = (
                await session.execute(
                    delete(DecisionLog).where(
                        (DecisionLog.captured_at < decision_cutoff)
                        & (DecisionLog.action != "BUY")
                    )
                )
            ).rowcount or 0
            await session.commit()

        self.stats.last_snapshots_deleted = snaps_deleted
        self.stats.last_decisions_deleted = decisions_deleted
        self.stats.total_snapshots_deleted += snaps_deleted
        self.stats.total_decisions_deleted += decisions_deleted
        self.stats.last_run_at = now
        self.stats.total_runs += 1
        self.stats.last_error = None
        logger.info(
            "RetentionMonitor cycle: -%d snapshots, -%d non-BUY decisions",
            snaps_deleted, decisions_deleted,
        )
