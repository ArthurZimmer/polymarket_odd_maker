"""In-process pub/sub for normalized odds snapshots.

Multiple consumers (EV engine, dashboard WS) can subscribe to the stream.
Slow consumers drop messages (bounded queue) rather than block the publisher.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OddsSnapshot:
    source: str                          # 'polymarket'|'pinnacle'|'bet365'|...
    event_id: str | None
    market_condition_id: str | None
    token_id: str | None
    outcome: str | None
    best_bid: float | None
    best_ask: float | None
    bid_depth_usd: float | None
    ask_depth_usd: float | None
    captured_at: datetime
    extra: dict = field(default_factory=dict)

    @property
    def mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None


class OddsBus:
    """Fan-out bus with bounded per-subscriber queues. Slow subscribers drop messages."""

    def __init__(self, default_queue_size: int = 1000) -> None:
        self._subscribers: list[asyncio.Queue[OddsSnapshot]] = []
        self._default_queue_size = default_queue_size
        self._dropped = 0

    def subscribe(self, maxsize: int | None = None) -> asyncio.Queue[OddsSnapshot]:
        q: asyncio.Queue[OddsSnapshot] = asyncio.Queue(maxsize=maxsize or self._default_queue_size)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[OddsSnapshot]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def publish(self, snapshot: OddsSnapshot) -> None:
        for q in self._subscribers:
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                self._dropped += 1
                if self._dropped % 1000 == 1:
                    logger.warning("OddsBus dropping messages — slow subscriber? total=%d", self._dropped)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped_count(self) -> int:
        return self._dropped
