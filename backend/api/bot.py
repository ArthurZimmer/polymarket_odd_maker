"""Bot master switch + risk knobs.

Single-row BotState is the source of truth read by TradingEngine and the
DecisionEngine. Mutating it via PATCH is the only safe way to flip
`is_running` — the lifespan never sets it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import require_auth
from backend.crypto.vault import VaultState
from backend.db import get_session
from backend.models import BotState

router = APIRouter(prefix="/api/bot", tags=["bot"])


class BotStateView(BaseModel):
    is_running: bool
    master_stake_usd: float
    ev_threshold: float
    exit_threshold: float
    stop_loss_pct: float
    max_concurrent_positions: int
    max_daily_drawdown_usd: float
    max_total_exposure_usd: float
    min_time_to_game_minutes: int
    max_time_to_game_minutes: int
    min_ask_depth_usd: float
    last_pause_reason: str | None
    last_paused_at: str | None
    vault_unlocked: bool
    updated_at: str


class BotStatePatch(BaseModel):
    is_running: bool | None = None
    master_stake_usd: float | None = Field(None, ge=1.0, le=10000.0)
    ev_threshold: float | None = Field(None, ge=0.0, le=1.0)
    exit_threshold: float | None = Field(None, ge=0.0, le=1.0)
    stop_loss_pct: float | None = Field(None, ge=0.0, le=1.0)
    max_concurrent_positions: int | None = Field(None, ge=1, le=50)
    max_daily_drawdown_usd: float | None = Field(None, ge=1.0, le=100000.0)
    max_total_exposure_usd: float | None = Field(None, ge=1.0, le=1000000.0)
    min_time_to_game_minutes: int | None = Field(None, ge=0, le=600)
    max_time_to_game_minutes: int | None = Field(None, ge=1, le=1440)
    min_ask_depth_usd: float | None = Field(None, ge=0.0, le=100000.0)


async def _get_or_create(session: AsyncSession) -> BotState:
    row = (
        await session.execute(select(BotState).where(BotState.id == 1))
    ).scalar_one_or_none()
    if row is None:
        row = BotState(id=1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def _to_view(row: BotState) -> BotStateView:
    return BotStateView(
        is_running=row.is_running,
        master_stake_usd=row.master_stake_usd,
        ev_threshold=row.ev_threshold,
        exit_threshold=row.exit_threshold,
        stop_loss_pct=row.stop_loss_pct,
        max_concurrent_positions=row.max_concurrent_positions,
        max_daily_drawdown_usd=row.max_daily_drawdown_usd,
        max_total_exposure_usd=row.max_total_exposure_usd,
        min_time_to_game_minutes=row.min_time_to_game_minutes,
        max_time_to_game_minutes=row.max_time_to_game_minutes,
        min_ask_depth_usd=row.min_ask_depth_usd,
        last_pause_reason=row.last_pause_reason,
        last_paused_at=row.last_paused_at.isoformat() if row.last_paused_at else None,
        vault_unlocked=VaultState.is_unlocked(),
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


@router.get("/state", response_model=BotStateView)
async def get_state(
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> BotStateView:
    return _to_view(await _get_or_create(session))


@router.patch("/state", response_model=BotStateView)
async def patch_state(
    payload: BotStatePatch,
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> BotStateView:
    row = await _get_or_create(session)
    data = payload.model_dump(exclude_unset=True)
    was_running = row.is_running
    for k, v in data.items():
        setattr(row, k, v)
    # User manually turning the bot back on after an auto-pause: clear the
    # pause flag so the dashboard banner goes away.
    if data.get("is_running") is True and not was_running:
        row.last_pause_reason = None
        row.last_paused_at = None
    await session.commit()
    await session.refresh(row)
    return _to_view(row)


@router.get("/trading-status")
async def trading_status(
    request: Request,
    _user: str = Depends(require_auth),
) -> dict:
    engine = getattr(request.app.state, "trading_engine", None)
    if engine is None:
        return {"running": False, "stats": None}
    return {"running": True, "stats": engine.stats.to_dict()}
