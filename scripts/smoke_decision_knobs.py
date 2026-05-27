"""Smoke for B-6: DecisionEngine reads BotState knobs every cycle.

Verifies _RuntimeKnobs.from_state surfaces ev_threshold, min/max window and
min_ask_depth_usd from BotState — not the module-level DEFAULT_* constants.

Run: .venv/bin/python -m scripts.smoke_decision_knobs
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from backend.db import SessionLocal
from backend.engine.decision import (
    DEFAULT_EV_THRESHOLD,
    DEFAULT_MASTER_STAKE_USD,
    _RuntimeKnobs,
)
from backend.models import BotState


async def main() -> None:
    async with SessionLocal() as session:
        state = (
            await session.execute(select(BotState).where(BotState.id == 1))
        ).scalar_one()
        original_ev = state.ev_threshold
        original_min = state.min_time_to_game_minutes
        original_max = state.max_time_to_game_minutes
        original_depth = state.min_ask_depth_usd
        original_stake = state.master_stake_usd

        # ---- 1. Knobs come from BotState ----
        state.ev_threshold = 0.10
        state.min_time_to_game_minutes = 15
        state.max_time_to_game_minutes = 60
        state.min_ask_depth_usd = 250.0
        state.master_stake_usd = 7.5
        await session.commit()
        await session.refresh(state)

        knobs = _RuntimeKnobs.from_state(state)
        print(f"[1] from BotState (ev=10%, win 15-60min, depth=$250, stake=$7.50):")
        print(f"    ev_threshold={knobs.ev_threshold}")
        print(f"    min_time_to_game_s={knobs.min_time_to_game_s}")
        print(f"    max_time_to_game_s={knobs.max_time_to_game_s}")
        print(f"    min_ask_depth_usd={knobs.min_ask_depth_usd}")
        print(f"    master_stake_usd={knobs.master_stake_usd}")
        assert knobs.ev_threshold == 0.10
        assert knobs.min_time_to_game_s == 15 * 60
        assert knobs.max_time_to_game_s == 60 * 60
        assert knobs.min_ask_depth_usd == 250.0
        assert knobs.master_stake_usd == 7.5

        # ---- 2. Different EV from default proves we're not hardcoded ----
        assert knobs.ev_threshold != DEFAULT_EV_THRESHOLD
        assert knobs.master_stake_usd != DEFAULT_MASTER_STAKE_USD

        # ---- 3. Falling back to defaults when state is None ----
        none_knobs = _RuntimeKnobs.from_state(None)
        print(f"\n[3] from_state(None) defaults: ev={none_knobs.ev_threshold} stake=${none_knobs.master_stake_usd}")
        assert none_knobs.ev_threshold == DEFAULT_EV_THRESHOLD
        assert none_knobs.master_stake_usd == DEFAULT_MASTER_STAKE_USD

        # Restore so we don't pollute the dev DB
        state.ev_threshold = original_ev
        state.min_time_to_game_minutes = original_min
        state.max_time_to_game_minutes = original_max
        state.min_ask_depth_usd = original_depth
        state.master_stake_usd = original_stake
        await session.commit()
        print("\nDone — BotState is now the source of truth for DecisionEngine.")


if __name__ == "__main__":
    asyncio.run(main())
