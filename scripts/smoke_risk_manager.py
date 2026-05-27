"""Smoke test for Etapa 11 risk gate.

Scenarios exercised:
  1. Force a CLOSED position with -150 USDC pnl (below default -100 limit)
     and confirm enforce_risk pauses the bot (is_running → False,
     last_pause_reason populated).
  2. Force concurrent count above limit (with an intended new order) and
     confirm CONCURRENT counts as serious + pauses.

Cleans up before and after. Restores is_running=False as left.

Run: .venv/bin/python -m scripts.smoke_risk_manager
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from backend.db import SessionLocal
from backend.models import BotState, Position
from backend.positions.risk import check_risk, enforce_risk

FAKE_TOKEN_PREFIX = "SMOKE_RISK_E11_"


async def cleanup(session) -> None:
    await session.execute(
        delete(Position).where(Position.token_id.like(f"{FAKE_TOKEN_PREFIX}%"))
    )
    await session.commit()


async def reset_bot_state(session) -> BotState:
    state = (
        await session.execute(select(BotState).where(BotState.id == 1))
    ).scalar_one()
    state.is_running = True
    state.last_pause_reason = None
    state.last_paused_at = None
    await session.commit()
    await session.refresh(state)
    return state


async def main() -> None:
    async with SessionLocal() as session:
        await cleanup(session)
        state = await reset_bot_state(session)
        print(
            f"Initial: is_running={state.is_running} "
            f"max_daily_drawdown=${state.max_daily_drawdown_usd} "
            f"max_total_exposure=${state.max_total_exposure_usd}"
        )

        # Scenario 1: realized drawdown -150 USDC > limit -100.
        loss_position = Position(
            polymarket_event_id="smoke_risk_event_drawdown",
            token_id=f"{FAKE_TOKEN_PREFIX}DRAW",
            outcome="Loser",
            size=100.0,
            entry_price=0.50,
            entry_at=datetime.now(UTC) - timedelta(hours=2),
            exit_price=0.05,
            exit_at=datetime.now(UTC).replace(tzinfo=None),
            pnl_usd=-150.0,
            status="CLOSED",
        )
        session.add(loss_position)
        await session.commit()

        report = await check_risk(session, state)
        print(
            f"\n[1] check_risk after CLOSED loss: passed={report.passed} "
            f"realized_today=${report.realized_pnl_today_usd:.2f} "
            f"violations={[v.code for v in report.violations]}"
        )
        assert report.passed is False
        assert any(v.code == "DRAWDOWN" for v in report.violations)

        # enforce_risk should now pause the bot.
        report2 = await enforce_risk(session, state)
        await session.commit()
        await session.refresh(state)
        print(
            f"[1] enforce_risk paused bot: is_running={state.is_running} "
            f"reason={state.last_pause_reason!r}"
        )
        assert state.is_running is False
        assert state.last_pause_reason is not None and "DRAWDOWN" in state.last_pause_reason.upper() or "prejuízo" in state.last_pause_reason
        # actually message is "prejuízo do dia ..."
        assert "prejuízo" in state.last_pause_reason

        # Scenario 2: re-enable, then test concurrent gate.
        await reset_bot_state(session)
        # Clear scenario 1 position so drawdown doesn't trip again.
        await cleanup(session)
        # Add enough OPEN positions to fill the concurrent slot at the limit.
        state = (
            await session.execute(select(BotState).where(BotState.id == 1))
        ).scalar_one()
        for i in range(state.max_concurrent_positions):
            session.add(
                Position(
                    polymarket_event_id=f"smoke_risk_event_conc_{i}",
                    token_id=f"{FAKE_TOKEN_PREFIX}CONC_{i}",
                    outcome="Side",
                    size=10.0,
                    entry_price=0.40,
                    entry_at=datetime.now(UTC),
                    status="OPEN",
                )
            )
        await session.commit()

        # check_risk without intended_notional — concurrent matches limit but
        # not exceeded, so it should still pass.
        r3 = await check_risk(session, state)
        print(
            f"\n[2] check_risk no intent: passed={r3.passed} "
            f"concurrent={r3.concurrent_count} violations={[v.code for v in r3.violations]}"
        )
        # With intended_notional > 0, concurrent +1 > limit → violation.
        r4 = await check_risk(session, state, intended_notional_usd=5.0)
        print(
            f"[2] check_risk +1 intent: passed={r4.passed} "
            f"violations={[v.code for v in r4.violations]}"
        )
        assert any(v.code == "CONCURRENT" for v in r4.violations)

        r5 = await enforce_risk(session, state, intended_notional_usd=5.0)
        await session.commit()
        await session.refresh(state)
        print(
            f"[2] enforce_risk paused for concurrent: is_running={state.is_running} "
            f"reason={state.last_pause_reason!r}"
        )
        assert state.is_running is False

        # Cleanup: clear positions + reset BotState to OFF.
        await cleanup(session)
        state.is_running = False
        state.last_pause_reason = None
        state.last_paused_at = None
        await session.commit()
        print("\n[3] Cleaned up. Done.")


if __name__ == "__main__":
    asyncio.run(main())
