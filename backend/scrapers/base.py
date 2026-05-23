"""Bookmaker scraper foundation: ABC, BackoffState, ScraperStats.

A concrete scraper subclasses BookmakerScraper and implements `_fetch_once()`,
which is invoked at a self-adjusting interval. Failures back off exponentially;
sustained failures degrade the scraper to OFFLINE for a cooldown window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from backend.engine.odds_bus import OddsBus

logger = logging.getLogger(__name__)


class ScraperHealth(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class BackoffState:
    """Adaptive interval + failure tracking. Reset on success."""

    base_interval_s: float = 10.0
    max_interval_s: float = 300.0
    current_interval_s: float = field(init=False)
    consecutive_failures: int = 0
    offline_until_ts: float | None = None

    def __post_init__(self) -> None:
        self.current_interval_s = self.base_interval_s

    def success(self) -> None:
        self.consecutive_failures = 0
        self.current_interval_s = self.base_interval_s
        self.offline_until_ts = None

    def failure(self) -> None:
        self.consecutive_failures += 1
        self.current_interval_s = min(
            self.max_interval_s, self.current_interval_s * 2
        )
        if self.consecutive_failures >= 5:
            # Cooldown: 10 min OFFLINE
            self.offline_until_ts = time.time() + 600

    def is_offline(self) -> bool:
        return self.offline_until_ts is not None and time.time() < self.offline_until_ts

    def health(self) -> ScraperHealth:
        if self.is_offline():
            return ScraperHealth.OFFLINE
        if self.consecutive_failures > 0:
            return ScraperHealth.DEGRADED
        return ScraperHealth.OK


@dataclass
class ScraperStats:
    name: str
    health: ScraperHealth = ScraperHealth.OK
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    interval_s: float = 10.0
    total_runs: int = 0
    total_failures: int = 0
    total_snapshots_published: int = 0
    snapshots_last_run: int = 0
    last_latency_ms: float | None = None
    _recent_run_ts: deque = field(default_factory=lambda: deque(maxlen=60))

    def runs_per_min(self) -> float:
        now = time.time()
        return float(sum(1 for t in self._recent_run_ts if t > now - 60))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "health": self.health.value,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "interval_s": self.interval_s,
            "total_runs": self.total_runs,
            "total_failures": self.total_failures,
            "total_snapshots_published": self.total_snapshots_published,
            "snapshots_last_run": self.snapshots_last_run,
            "last_latency_ms": self.last_latency_ms,
            "runs_per_min": self.runs_per_min(),
        }


class BookmakerScraper(ABC):
    """Periodic scraper for one bookmaker. Pull-based (no WS), adaptive interval."""

    name: str = "unnamed"

    def __init__(
        self,
        bus: OddsBus,
        session_factory,
        *,
        base_interval_s: float = 15.0,
        max_interval_s: float = 300.0,
    ) -> None:
        self.bus = bus
        self.session_factory = session_factory
        self.backoff = BackoffState(base_interval_s=base_interval_s, max_interval_s=max_interval_s)
        self.stats = ScraperStats(name=self.name, interval_s=base_interval_s)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("Scraper %s starting", self.name)
        while not self._stop.is_set():
            if self.backoff.is_offline():
                self.stats.health = ScraperHealth.OFFLINE
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=max(30.0, self.backoff.offline_until_ts - time.time()),  # type: ignore[operator]
                    )
                except asyncio.TimeoutError:
                    self.backoff.offline_until_ts = None  # try again
                    self.backoff.consecutive_failures = 0
                continue

            run_started = time.time()
            self.stats.total_runs += 1
            self.stats.snapshots_last_run = 0
            self.stats.last_run_at = datetime.now(UTC)
            self.stats._recent_run_ts.append(run_started)

            try:
                snapshots_count = await self._fetch_once()
                self.stats.snapshots_last_run = snapshots_count
                self.stats.total_snapshots_published += snapshots_count
                self.stats.last_success_at = datetime.now(UTC)
                self.stats.last_error = None
                self.backoff.success()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Scraper %s failed", self.name)
                self.stats.total_failures += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.backoff.failure()

            self.stats.consecutive_failures = self.backoff.consecutive_failures
            self.stats.interval_s = self.backoff.current_interval_s
            self.stats.health = self.backoff.health()
            self.stats.last_latency_ms = (time.time() - run_started) * 1000.0

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.backoff.current_interval_s
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Scraper %s stopped", self.name)

    @abstractmethod
    async def _fetch_once(self) -> int:
        """Pull current odds. Publish snapshots to the bus. Return # snapshots published."""
        raise NotImplementedError
