"""Smoke test for the Etapa 10 PositionManager paths.

Creates a synthetic Position + polymarket snapshot in the DB, then exercises:
  1. manual_close_position with no bid    → 'no recent polymarket bid'
  2. Inserts a bid that triggers stop-loss
     and verifies the evaluation reaches submit_exit_order.

The CLOB submission itself will fail because we don't have a real wallet —
that's fine. We're verifying the *gate* logic up to the submit attempt.

Run: .venv/bin/python -m scripts.smoke_position_manager
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from backend.crypto.vault import VaultState
from backend.db import SessionLocal
from backend.engine.position_manager import (
    PositionManager,
    manual_close_position,
)
from backend.models import (
    BotState,
    OddsSnapshot,
    Order,
    Position,
)

FAKE_TOKEN = "SMOKE_TOKEN_PM_E10"
FAKE_PM_EVENT = "smoke_pm_event_e10"


async def cleanup(session) -> None:
    await session.execute(
        delete(Order).where(Order.token_id == FAKE_TOKEN)
    )
    await session.execute(
        delete(Position).where(Position.token_id == FAKE_TOKEN)
    )
    await session.execute(
        delete(OddsSnapshot).where(OddsSnapshot.token_id == FAKE_TOKEN)
    )
    await session.commit()


async def main() -> None:
    # Unlock vault so submit_exit_order reaches place_limit_order (which will
    # fail at the CLOB layer without real creds, but we want to verify that
    # the Order row is persisted).
    try:
        VaultState.unlock("test-password-456")
        print(f"vault unlocked: {VaultState.is_unlocked()}")
    except Exception as e:
        print(f"vault unlock failed (ok, continuing locked): {e}")

    async with SessionLocal() as session:
        await cleanup(session)
        bot = (
            await session.execute(select(BotState).where(BotState.id == 1))
        ).scalar_one_or_none()
        assert bot is not None
        print(f"BotState.stop_loss_pct = {bot.stop_loss_pct}")
        print(f"BotState.exit_threshold = {bot.exit_threshold}")

        # Create a synthetic OPEN position. entry_price 0.50, size 10.
        pos = Position(
            polymarket_event_id=FAKE_PM_EVENT,
            token_id=FAKE_TOKEN,
            outcome="Smoke Team A",
            size=10.0,
            entry_price=0.50,
            entry_at=datetime.now(UTC),
            status="OPEN",
        )
        session.add(pos)
        await session.commit()
        await session.refresh(pos)
        print(f"\n[1] Created position id={pos.id}")

        # 1. Manual close with no snapshot → should return False, no bid msg.
        ok, msg = await manual_close_position(session, pos.id)
        print(f"[1a] manual_close no-snapshot: ok={ok} msg={msg!r}")
        assert ok is False and "polymarket bid" in msg

        # 2. Insert a polymarket snapshot with bid above stop-loss but
        #    way below entry → no trigger expected.
        session.add(
            OddsSnapshot(
                captured_at=datetime.now(UTC).replace(tzinfo=None),
                source="polymarket",
                event_id="polymarket:smoke",
                outcome="Smoke Team A",
                token_id=FAKE_TOKEN,
                best_bid=0.48,
                best_ask=0.49,
                ask_depth_usd=200.0,
            )
        )
        await session.commit()

        # Run one PositionManager cycle and inspect.
        mgr = PositionManager(SessionLocal)
        await mgr._cycle()
        print(f"\n[2] PM cycle stats: {mgr.stats.last_actions}")
        # Expected: this position has no EventMatch → falls into 'no_match'
        # branch (stop-loss didn't trigger at 0.48 > 0.50 * 0.7 = 0.35).

        # 3. Now drop bid to trigger stop-loss.
        await session.execute(
            delete(OddsSnapshot).where(OddsSnapshot.token_id == FAKE_TOKEN)
        )
        session.add(
            OddsSnapshot(
                captured_at=datetime.now(UTC).replace(tzinfo=None),
                source="polymarket",
                event_id="polymarket:smoke",
                outcome="Smoke Team A",
                token_id=FAKE_TOKEN,
                best_bid=0.30,  # 40% below entry 0.50 → stop-loss at 30% fires
                best_ask=0.31,
                ask_depth_usd=200.0,
            )
        )
        await session.commit()

        mgr2 = PositionManager(SessionLocal)
        await mgr2._cycle()
        print(f"[3] PM cycle stats (stop-loss expected): {mgr2.stats.last_actions}")

        # Force re-read from DB via a fresh session.
        async with SessionLocal() as fresh:
            pos = (
                await fresh.execute(select(Position).where(Position.token_id == FAKE_TOKEN))
            ).scalar_one()
            print(
                f"[3] Position after cycle: status={pos.status} exit_order_id={pos.exit_order_id}"
            )
            if pos.exit_order_id is not None:
                order = await fresh.get(Order, pos.exit_order_id)
                if order:
                    print(
                        f"[3] Exit order: side={order.side} status={order.status} "
                        f"price={order.price} size={order.size} err={order.last_error!r}"
                    )
        # If wallet/CLOB is configured, exit_order_id is set + Order is SUBMITTED
        # or FAILED. If not, the order was created in DB then failed during
        # place_limit_order — exit_order_id is still set.

        # 4. Cleanup
        await cleanup(session)
        print("\n[4] Cleaned up. Done.")


if __name__ == "__main__":
    asyncio.run(main())
