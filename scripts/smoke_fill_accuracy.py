"""Smoke for B-1/B-2/B-3: avg_price real + partial-fill state.

Scenarios:
  1. BUY filled at avg_price=0.48 below limit=0.50 → Position.entry_price=0.48
     (the *real* avg), not the limit price.
  2. SELL with size_matched=4 of size=10 in status='matched' → Order goes to
     PARTIAL (not FILLED); Position stays OPEN.
  3. Then status='filled' with size_matched=10 → Order becomes FILLED,
     Position closes with pnl_usd computed against the SELL avg_price.

Run: .venv/bin/python -m scripts.smoke_fill_accuracy
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete, select

from backend.db import SessionLocal
from backend.engine.trading import TradingEngine
from backend.models import Order, Position

FAKE_TOKEN = "SMOKE_FILL_ACC"


class FakeCLOBResponse:
    """Stand-in for what `get_order` returns from py-clob-client."""

    def __init__(self, status: str, size_matched: float, price_matched: float | None = None):
        self.payload: dict = {"status": status, "size_matched": size_matched}
        if price_matched is not None:
            self.payload["price_matched"] = price_matched

    def get(self, key, default=None):
        return self.payload.get(key, default)


async def cleanup(session) -> None:
    await session.execute(delete(Position).where(Position.token_id == FAKE_TOKEN))
    await session.execute(delete(Order).where(Order.token_id == FAKE_TOKEN))
    await session.commit()


async def make_buy_order(session) -> Order:
    o = Order(
        polymarket_order_id="pm_smoke_buy_1",
        token_id=FAKE_TOKEN,
        outcome="Side",
        side="BUY",
        price=0.50,
        size=10.0,
        notional_usd=5.0,
        order_type="GTC",
        status="SUBMITTED",
        submitted_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(o)
    await session.commit()
    await session.refresh(o)
    return o


async def main() -> None:
    engine = TradingEngine(SessionLocal)
    async with SessionLocal() as session:
        await cleanup(session)

        # ---- Scenario 1: BUY fills at price < limit ----
        order = await make_buy_order(session)
        resp = FakeCLOBResponse("filled", size_matched=10.0, price_matched=0.48)
        from backend.polymarket.clob_client import _extract_fill_data
        size_matched, avg_price = _extract_fill_data(resp.payload, order.price)
        print(f"[1] BUY fill: size_matched={size_matched} avg={avg_price} (limit=0.50)")
        assert avg_price == 0.48, f"expected avg=0.48, got {avg_price}"

        # Run _mark_filled directly to test integration
        await engine._mark_filled(session, order, filled_size=size_matched, avg_price=avg_price)
        await session.commit()
        pos = (
            await session.execute(select(Position).where(Position.token_id == FAKE_TOKEN))
        ).scalar_one()
        print(f"[1] Position created: entry_price={pos.entry_price} size={pos.size}")
        assert pos.entry_price == 0.48, "entry_price should be the avg fill, not limit"
        assert pos.size == 10.0
        assert pos.status == "OPEN"

        # ---- Scenario 2: SELL with partial fill (size_matched=4, size=10, status=matched) ----
        sell_order = Order(
            polymarket_order_id="pm_smoke_sell_1",
            token_id=FAKE_TOKEN,
            outcome="Side",
            side="SELL",
            price=0.55,
            size=10.0,
            notional_usd=5.5,
            order_type="GTC",
            status="SUBMITTED",
            submitted_at=datetime.now(UTC).replace(tzinfo=None),
            exit_policy="ride",
        )
        session.add(sell_order)
        await session.commit()
        await session.refresh(sell_order)
        pos.exit_order_id = sell_order.id
        await session.commit()

        # Simulate poller logic: matched + 4/10 → _mark_partial
        await engine._mark_partial(session, sell_order, 4.0, 0.55)
        await session.commit()
        await session.refresh(sell_order)
        await session.refresh(pos)
        print(f"[2] SELL partial: status={sell_order.status} filled={sell_order.filled_size}")
        assert sell_order.status == "PARTIAL", "should be PARTIAL not FILLED"
        assert sell_order.filled_size == 4.0
        print(f"[2] Position still OPEN: status={pos.status} size={pos.size}")
        assert pos.status == "OPEN"
        assert pos.size == 10.0, "Position size unchanged while order in flight"

        # ---- Scenario 3: SELL completes fully ----
        await engine._mark_filled(session, sell_order, filled_size=10.0, avg_price=0.55)
        await session.commit()
        await session.refresh(sell_order)
        await session.refresh(pos)
        print(f"[3] SELL FILLED: status={sell_order.status}")
        print(f"[3] Position CLOSED: status={pos.status} pnl_usd={pos.pnl_usd}")
        assert sell_order.status == "FILLED"
        assert pos.status == "CLOSED"
        # PnL = (0.55 - 0.48) * 10 = 0.70
        assert abs(pos.pnl_usd - 0.70) < 0.01, f"expected pnl≈0.70 got {pos.pnl_usd}"

        await cleanup(session)
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
