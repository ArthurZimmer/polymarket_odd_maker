"""Risk status endpoint — read-only view of the current gate report."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import require_auth
from backend.db import get_session
from backend.models import BotState
from backend.polymarket.clob_client import get_usdc_balance
from backend.positions.risk import check_risk

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/status")
async def risk_status(
    request: Request,
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    state = (
        await session.execute(select(BotState).where(BotState.id == 1))
    ).scalar_one_or_none()
    if state is None:
        return {"report": None, "monitor": None}

    bankroll = await get_usdc_balance(session)
    report = await check_risk(session, state, bankroll_usd=bankroll)
    monitor = getattr(request.app.state, "risk_monitor", None)
    return {
        "report": report.to_dict(),
        "monitor": monitor.stats.to_dict() if monitor else None,
    }
