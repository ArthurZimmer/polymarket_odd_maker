"""Smoke for B-8: PositionManager refresh + re-check protects against the
poller closing a position mid-cycle.

Reproduces the race symbolically:
  1. PM session loads Position p (status=OPEN).
  2. Another session (the "poller") marks p CLOSED.
  3. PM calls _evaluate_position(p) — must refresh from DB, detect CLOSED,
     and return "already_closed" without submitting any SELL.

Run: .venv/bin/python -m scripts.smoke_race_position_manager
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete, select

from backend.db import SessionLocal
from backend.engine.position_manager import PositionManager
from backend.models import BotState, Order, Position

FAKE_TOKEN = "SMOKE_RACE_PM"


async def cleanup(session) -> None:
    await session.execute(delete(Position).where(Position.token_id == FAKE_TOKEN))
    await session.execute(delete(Order).where(Order.token_id == FAKE_TOKEN))
    await session.commit()


async def main() -> None:
    async with SessionLocal() as setup:
        await cleanup(setup)
        p = Position(
            polymarket_event_id="smoke_race_event",
            token_id=FAKE_TOKEN,
            outcome="Side",
            size=10.0,
            entry_price=0.50,
            entry_at=datetime.now(UTC).replace(tzinfo=None),
            status="OPEN",
        )
        setup.add(p)
        await setup.commit()
        await setup.refresh(p)
        pos_id = p.id

    # PM session loads the position (status=OPEN at this moment)
    pm_session = SessionLocal()
    try:
        async with pm_session as pm:
            pm_pos = await pm.get(Position, pos_id)
            assert pm_pos is not None
            print(f"[1] PM loaded position: status={pm_pos.status}")
            assert pm_pos.status == "OPEN"

            # Simulate the poller closing it in *another* session.
            async with SessionLocal() as poller:
                p2 = await poller.get(Position, pos_id)
                p2.status = "CLOSED"
                p2.exit_at = datetime.now(UTC).replace(tzinfo=None)
                p2.exit_price = 0.55
                p2.pnl_usd = 0.50
                await poller.commit()
            print("[2] Poller closed position in another session.")

            # Now PM evaluates — must refresh and bail.
            state = (
                await pm.execute(select(BotState).where(BotState.id == 1))
            ).scalar_one()
            mgr = PositionManager(SessionLocal)
            action = await mgr._evaluate_position(
                pm, pm_pos, state, datetime.now(UTC)
            )
            print(f"[3] _evaluate_position result: {action!r}")
            assert action == "already_closed", \
                f"expected 'already_closed', got {action!r}"

            # No new Order should have been added for this token
            orders = (
                await pm.execute(select(Order).where(Order.token_id == FAKE_TOKEN))
            ).scalars().all()
            print(f"[4] orders for token: {len(orders)} (expected 0)")
            assert len(orders) == 0
    finally:
        async with SessionLocal() as cln:
            await cleanup(cln)
    print("\nDone — race-safe, no SELL submitted for already-closed position.")


if __name__ == "__main__":
    asyncio.run(main())
