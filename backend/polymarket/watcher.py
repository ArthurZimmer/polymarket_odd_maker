"""Long-running Polymarket WebSocket watcher.

Lifecycle:
  - Run in an asyncio Task created at lifespan start.
  - Outer loop: resolve filters → connect WS → handle messages → reconnect on close.
  - Inner task: every 60s, re-resolve filters; if subscription set changed, close
    the WS (triggering outer-loop reconnect with the new set).

Each WS message is normalized to OddsSnapshot, persisted to `odds_snapshots`,
and published on the OddsBus for downstream consumers (EV engine, dashboard).

Stats are exposed via `watcher.stats.to_dict()` for the /api/watcher/status
endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

import websockets

from backend.engine.odds_bus import OddsBus, OddsSnapshot
from backend.models import OddsSnapshot as OddsSnapshotRow
from backend.polymarket.resolver import SubscriptionPlan, TokenSpec, resolve_subscriptions

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RESUBSCRIBE_CHECK_INTERVAL = 60.0
WS_OPEN_TIMEOUT = 15.0
WS_PING_INTERVAL = 20.0
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0


class WatcherStats:
    def __init__(self) -> None:
        self.connected: bool = False
        self.connected_at: datetime | None = None
        self.last_disconnect_at: datetime | None = None
        self.last_disconnect_reason: str | None = None
        self.subscribed_tokens: int = 0
        self.subscribed_events: int = 0
        self.subscription_truncated: bool = False
        self.total_messages: int = 0
        self._msg_timestamps: deque[float] = deque(maxlen=600)
        self.last_message_at: datetime | None = None

    def mark_connected(self) -> None:
        self.connected = True
        self.connected_at = datetime.now(UTC)

    def mark_disconnected(self, reason: str | None = None) -> None:
        self.connected = False
        self.last_disconnect_at = datetime.now(UTC)
        self.last_disconnect_reason = reason

    def tick(self) -> None:
        self.total_messages += 1
        now = time.time()
        self._msg_timestamps.append(now)
        self.last_message_at = datetime.now(UTC)

    def updates_per_min(self) -> float:
        now = time.time()
        cutoff = now - 60.0
        recent = sum(1 for t in self._msg_timestamps if t > cutoff)
        return float(recent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_disconnect_at": self.last_disconnect_at.isoformat() if self.last_disconnect_at else None,
            "last_disconnect_reason": self.last_disconnect_reason,
            "subscribed_tokens": self.subscribed_tokens,
            "subscribed_events": self.subscribed_events,
            "subscription_truncated": self.subscription_truncated,
            "total_messages": self.total_messages,
            "updates_per_min": self.updates_per_min(),
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
        }


class PolymarketWatcher:
    def __init__(self, bus: OddsBus, session_factory) -> None:
        self.bus = bus
        self.session_factory = session_factory
        self.stats = WatcherStats()
        self._stop = asyncio.Event()
        self._token_index: dict[str, TokenSpec] = {}

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempts = 0
        while not self._stop.is_set():
            try:
                ran = await self._cycle()
                if ran:
                    attempts = 0  # successful cycle resets backoff
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watcher cycle crashed")
            finally:
                self.stats.mark_disconnected("cycle_end")
            if self._stop.is_set():
                break
            delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** min(attempts, 6)))
            attempts += 1
            logger.info("Watcher reconnect in %.1fs (attempt %d)", delay, attempts)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> bool:
        """One connect→consume→close cycle. Returns True if we actually opened a WS."""
        plan = await self._resolve_with_session()
        self._token_index = {t.token_id: t for t in plan.tokens}
        self.stats.subscribed_tokens = len(plan.tokens)
        self.stats.subscribed_events = plan.event_count
        self.stats.subscription_truncated = plan.truncated

        if not plan.tokens:
            logger.info("Watcher idle — no filters selected")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RESUBSCRIBE_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
            return False

        logger.info(
            "Watcher connecting: %d tokens / %d events (truncated=%s)",
            plan.tokens.__len__(),
            plan.event_count,
            plan.truncated,
        )
        try:
            async with websockets.connect(
                WS_URL,
                open_timeout=WS_OPEN_TIMEOUT,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({"type": "MARKET", "assets_ids": list(self._token_index.keys())}))
                self.stats.mark_connected()
                resub_task = asyncio.create_task(self._periodic_resubscribe_check(ws))
                try:
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle_msg(raw)
                finally:
                    resub_task.cancel()
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("WS closed: %s", exc)
            self.stats.mark_disconnected(f"connection_closed: {exc.code}")
        return True

    async def _resolve_with_session(self) -> SubscriptionPlan:
        async with self.session_factory() as session:
            return await resolve_subscriptions(session)

    async def _periodic_resubscribe_check(self, ws) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=RESUBSCRIBE_CHECK_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                else:
                    return
                plan = await self._resolve_with_session()
                new_ids = {t.token_id for t in plan.tokens}
                if new_ids != set(self._token_index.keys()):
                    logger.info(
                        "Filter set changed (%d -> %d tokens) — closing WS to resubscribe",
                        len(self._token_index),
                        len(new_ids),
                    )
                    await ws.close()
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_msg(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.stats.tick()
        if isinstance(data, list):
            # Initial book snapshots
            for book in data:
                if isinstance(book, dict):
                    await self._process_book(book)
        elif isinstance(data, dict):
            if "price_changes" in data:
                await self._process_price_changes(data)

    async def _process_book(self, book: dict[str, Any]) -> None:
        token_id = book.get("asset_id")
        if not token_id:
            return
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid_p, best_bid_size = _best_level(bids, descending=True)
        best_ask_p, best_ask_size = _best_level(asks, descending=False)
        snap = self._make_snapshot(
            token_id=token_id,
            market_condition_id=book.get("market"),
            best_bid=best_bid_p,
            best_ask=best_ask_p,
            bid_depth_usd=_depth_to_usd(best_bid_p, best_bid_size),
            ask_depth_usd=_depth_to_usd(best_ask_p, best_ask_size),
        )
        await self._emit(snap)

    async def _process_price_changes(self, msg: dict[str, Any]) -> None:
        market_cond_id = msg.get("market")
        for chg in msg.get("price_changes") or []:
            token_id = chg.get("asset_id")
            if not token_id:
                continue
            best_bid = _maybe_float(chg.get("best_bid"))
            best_ask = _maybe_float(chg.get("best_ask"))
            snap = self._make_snapshot(
                token_id=token_id,
                market_condition_id=market_cond_id,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_depth_usd=None,
                ask_depth_usd=None,
            )
            await self._emit(snap)

    def _make_snapshot(
        self,
        *,
        token_id: str,
        market_condition_id: str | None,
        best_bid: float | None,
        best_ask: float | None,
        bid_depth_usd: float | None,
        ask_depth_usd: float | None,
    ) -> OddsSnapshot:
        spec = self._token_index.get(token_id)
        return OddsSnapshot(
            source="polymarket",
            event_id=spec.event_id if spec else None,
            market_condition_id=market_condition_id,
            token_id=token_id,
            outcome=spec.outcome if spec else None,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth_usd=bid_depth_usd,
            ask_depth_usd=ask_depth_usd,
            captured_at=datetime.now(UTC),
        )

    async def _emit(self, snap: OddsSnapshot) -> None:
        await self.bus.publish(snap)
        # Persist (best-effort; failures are logged but don't crash the loop)
        try:
            async with self.session_factory() as session:
                session.add(
                    OddsSnapshotRow(
                        source=snap.source,
                        event_id=snap.event_id,
                        market_condition_id=snap.market_condition_id,
                        token_id=snap.token_id,
                        outcome=snap.outcome,
                        best_bid=snap.best_bid,
                        best_ask=snap.best_ask,
                        mid_price=snap.mid_price,
                        bid_depth_usd=snap.bid_depth_usd,
                        ask_depth_usd=snap.ask_depth_usd,
                        captured_at=snap.captured_at,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to persist OddsSnapshot")


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _best_level(levels: list[dict[str, Any]], *, descending: bool) -> tuple[float | None, float | None]:
    """levels is a list of {price, size} strings. Return (best_price, best_size)."""
    parsed: list[tuple[float, float]] = []
    for lv in levels:
        p = _maybe_float(lv.get("price"))
        s = _maybe_float(lv.get("size"))
        if p is None or s is None:
            continue
        parsed.append((p, s))
    if not parsed:
        return None, None
    parsed.sort(key=lambda x: x[0], reverse=descending)
    return parsed[0]


def _depth_to_usd(price: float | None, size: float | None) -> float | None:
    if price is None or size is None:
        return None
    return price * size
