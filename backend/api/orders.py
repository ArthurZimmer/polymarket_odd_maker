"""Orders + positions read endpoints + manual position close."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import require_auth
from backend.db import get_session
from backend.engine.position_manager import manual_close_position
from backend.models import Order, Position

router = APIRouter(prefix="/api", tags=["orders"])


class OrderRow(BaseModel):
    id: int
    polymarket_order_id: str | None
    polymarket_event_id: str | None
    token_id: str
    outcome: str | None
    side: str
    price: float
    size: float
    notional_usd: float
    order_type: str
    status: str
    filled_size: float
    filled_avg_price: float | None
    decision_id: int | None
    last_error: str | None
    created_at: str
    submitted_at: str | None
    filled_at: str | None
    cancelled_at: str | None


class PositionRow(BaseModel):
    id: int
    polymarket_event_id: str | None
    token_id: str
    outcome: str | None
    size: float
    entry_price: float
    entry_at: str
    exit_price: float | None
    exit_at: str | None
    pnl_usd: float | None
    status: str
    entry_order_id: int | None
    exit_order_id: int | None


@router.get("/orders/recent", response_model=list[OrderRow])
async def recent_orders(
    limit: int = Query(50, ge=1, le=500),
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> list[OrderRow]:
    rows = (
        await session.execute(select(Order).order_by(desc(Order.id)).limit(limit))
    ).scalars().all()
    return [
        OrderRow(
            id=r.id,
            polymarket_order_id=r.polymarket_order_id,
            polymarket_event_id=r.polymarket_event_id,
            token_id=r.token_id,
            outcome=r.outcome,
            side=r.side,
            price=r.price,
            size=r.size,
            notional_usd=r.notional_usd,
            order_type=r.order_type,
            status=r.status,
            filled_size=r.filled_size,
            filled_avg_price=r.filled_avg_price,
            decision_id=r.decision_id,
            last_error=r.last_error,
            created_at=r.created_at.isoformat() if r.created_at else "",
            submitted_at=r.submitted_at.isoformat() if r.submitted_at else None,
            filled_at=r.filled_at.isoformat() if r.filled_at else None,
            cancelled_at=r.cancelled_at.isoformat() if r.cancelled_at else None,
        )
        for r in rows
    ]


def _position_to_row(r: Position) -> PositionRow:
    return PositionRow(
        id=r.id,
        polymarket_event_id=r.polymarket_event_id,
        token_id=r.token_id,
        outcome=r.outcome,
        size=r.size,
        entry_price=r.entry_price,
        entry_at=r.entry_at.isoformat() if r.entry_at else "",
        exit_price=r.exit_price,
        exit_at=r.exit_at.isoformat() if r.exit_at else None,
        pnl_usd=r.pnl_usd,
        status=r.status,
        entry_order_id=r.entry_order_id,
        exit_order_id=r.exit_order_id,
    )


@router.get("/positions/open", response_model=list[PositionRow])
async def open_positions(
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> list[PositionRow]:
    rows = (
        await session.execute(
            select(Position).where(Position.status == "OPEN").order_by(desc(Position.id))
        )
    ).scalars().all()
    return [_position_to_row(r) for r in rows]


@router.get("/positions/recent", response_model=list[PositionRow])
async def recent_positions(
    limit: int = Query(50, ge=1, le=500),
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> list[PositionRow]:
    rows = (
        await session.execute(
            select(Position).order_by(desc(Position.id)).limit(limit)
        )
    ).scalars().all()
    return [_position_to_row(r) for r in rows]


class CloseResult(BaseModel):
    success: bool
    message: str
    position: PositionRow | None = None


@router.post("/positions/{position_id}/close", response_model=CloseResult)
async def close_position(
    position_id: int,
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> CloseResult:
    ok, msg = await manual_close_position(session, position_id)
    row = (
        await session.execute(select(Position).where(Position.id == position_id))
    ).scalar_one_or_none()
    if row is None and not ok:
        raise HTTPException(status_code=404, detail=msg)
    return CloseResult(
        success=ok,
        message=msg,
        position=_position_to_row(row) if row else None,
    )


@router.get("/positions/manager-status")
async def position_manager_status(
    request: Request,
    _user: str = Depends(require_auth),
) -> dict:
    mgr = getattr(request.app.state, "position_manager", None)
    if mgr is None:
        return {"running": False, "stats": None}
    return {"running": True, "stats": mgr.stats.to_dict()}
