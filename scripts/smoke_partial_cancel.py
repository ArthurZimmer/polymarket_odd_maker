"""Smoke for B-9: partial-fill SELL cancellation decrements Position.size
and accumulates realized PnL in realized_pnl_partial_usd.

Setup: Position size=10 @ entry=0.50, exit_order_id linked to a SELL with
filled_size=4 @ avg=0.55 that gets cancelled by the poller. After
_mark_partial_cancelled the Position must:
  - status stays OPEN
  - size = 6 (10 - 4)
  - realized_pnl_partial_usd ≈ (0.55 - 0.50) * 4 = 0.20
  - exit_order_id = None (so PM can retry on what's left)

Run: .venv/bin/python -m scripts.smoke_partial_cancel
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete, select

from backend.db import SessionLocal
from backend.engine.trading import TradingEngine
from backend.models import Order, Position

FAKE_TOKEN = "SMOKE_PARTIAL_CANCEL"


async def cleanup(session) -> None:
    await session.execute(delete(Position).where(Position.token_id == FAKE_TOKEN))
    await session.execute(delete(Order).where(Order.token_id == FAKE_TOKEN))
    await session.commit()


async def main() -> None:
    engine = TradingEngine(SessionLocal)
    async with SessionLocal() as session:
        await cleanup(session)
        pos = Position(
            polymarket_event_id="smoke_partial_event",
            token_id=FAKE_TOKEN,
            outcome="Side",
            size=10.0,
            entry_price=0.50,
            entry_at=datetime.now(UTC).replace(tzinfo=None),
            status="OPEN",
        )
        session.add(pos)
        await session.commit()
        await session.refresh(pos)

        sell = Order(
            polymarket_order_id="pm_partial_1",
            token_id=FAKE_TOKEN,
            outcome="Side",
            side="SELL",
            price=0.55,
            size=10.0,
            notional_usd=5.5,
            order_type="GTC",
            status="PARTIAL",
            filled_size=4.0,
            filled_avg_price=0.55,
            submitted_at=datetime.now(UTC).replace(tzinfo=None),
            exit_policy="cancel",
        )
        session.add(sell)
        await session.commit()
        await session.refresh(sell)
        pos.exit_order_id = sell.id
        await session.commit()

        print(
            f"[1] Pre-cancel: Position size={pos.size} pnl_partial={pos.realized_pnl_partial_usd}, "
            f"Order filled={sell.filled_size}/{sell.size}"
        )

        # Apply partial cancel
        await engine._mark_partial_cancelled(
            session, sell,
            partial_size=4.0,
            partial_avg=0.55,
            reason="stale > 600s",
        )
        await session.commit()
        await session.refresh(pos)
        await session.refresh(sell)

        print(
            f"[2] Post-cancel: Position status={pos.status} size={pos.size} "
            f"realized_pnl_partial=${pos.realized_pnl_partial_usd:.4f} "
            f"exit_order_id={pos.exit_order_id}"
        )
        print(f"[2] Order status={sell.status} reason={sell.last_error!r}")
        assert pos.status == "OPEN"
        assert abs(pos.size - 6.0) < 1e-6, f"expected size=6, got {pos.size}"
        assert abs(pos.realized_pnl_partial_usd - 0.20) < 0.01
        assert pos.exit_order_id is None
        assert sell.status == "CANCELLED"

        # Now simulate a follow-up SELL that completes the rest, must close
        # the position with PnL = partial + final
        followup = Order(
            polymarket_order_id="pm_partial_2",
            token_id=FAKE_TOKEN,
            outcome="Side",
            side="SELL",
            price=0.60,
            size=6.0,
            notional_usd=3.6,
            order_type="GTC",
            status="SUBMITTED",
            submitted_at=datetime.now(UTC).replace(tzinfo=None),
            exit_policy="ride",
        )
        session.add(followup)
        await session.commit()
        await session.refresh(followup)
        pos.exit_order_id = followup.id
        await session.commit()

        await engine._mark_filled(session, followup, filled_size=6.0, avg_price=0.60)
        await session.commit()
        await session.refresh(pos)
        print(
            f"[3] After final fill: status={pos.status} pnl_usd={pos.pnl_usd} "
            f"(partial=0.20 + final={(0.60-0.50)*6})"
        )
        # Expected pnl = 0.20 (partial) + 0.60 (final) = 0.80
        assert pos.status == "CLOSED"
        assert abs(pos.pnl_usd - 0.80) < 0.01, f"expected 0.80, got {pos.pnl_usd}"

        await cleanup(session)
        print("\nDone — partial-fill cancel decrements size + PnL rolls up correctly.")


if __name__ == "__main__":
    asyncio.run(main())
