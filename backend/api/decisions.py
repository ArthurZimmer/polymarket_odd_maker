"""DecisionEngine status + recent decisions feed."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import require_auth
from backend.db import get_session
from backend.models import DecisionLog

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


class EngineStatus(BaseModel):
    last_run_at: str | None
    last_run_duration_ms: float | None
    total_runs: int
    last_evaluations: int
    last_buys: int
    last_passes_by_reason: dict[str, int]
    total_buys: int
    total_passes: int
    total_decisions: int
    dry_run: bool


class DecisionRow(BaseModel):
    id: int
    captured_at: str
    polymarket_event_id: str
    polymarket_token_id: str | None
    pm_outcome: str | None
    outcome_side: str | None
    sport: str | None
    league: str | None
    pm_event_title: str | None
    action: str
    reason: str | None
    fair_prob: float | None
    poly_best_bid: float | None
    poly_best_ask: float | None
    poly_ask_depth_usd: float | None
    pinnacle_decimal_odd: float | None
    ev: float | None
    proposed_stake_usd: float | None
    proposed_price: float | None
    seconds_to_kickoff: float | None


@router.get("/status", response_model=EngineStatus)
async def status(
    request: Request,
    _user: str = Depends(require_auth),
) -> EngineStatus:
    engine = getattr(request.app.state, "decision_engine", None)
    if engine is None:
        return EngineStatus(
            last_run_at=None,
            last_run_duration_ms=None,
            total_runs=0,
            last_evaluations=0,
            last_buys=0,
            last_passes_by_reason={},
            total_buys=0,
            total_passes=0,
            total_decisions=0,
            dry_run=True,
        )
    d = engine.stats.to_dict()
    d["dry_run"] = engine.dry_run
    return EngineStatus(**d)


@router.get("/recent", response_model=list[DecisionRow])
async def recent(
    limit: int = Query(50, ge=1, le=500),
    action: str | None = Query(None, description="Comma-separated action filter, e.g. BUY,PASS_LOW_EV"),
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> list[DecisionRow]:
    q = select(DecisionLog).order_by(desc(DecisionLog.id)).limit(limit)
    if action:
        wanted = [a.strip() for a in action.split(",") if a.strip()]
        if wanted:
            q = q.where(DecisionLog.action.in_(wanted))
    rows = (await session.execute(q)).scalars().all()
    return [
        DecisionRow(
            id=r.id,
            captured_at=r.captured_at.isoformat() if r.captured_at else "",
            polymarket_event_id=r.polymarket_event_id,
            polymarket_token_id=r.polymarket_token_id,
            pm_outcome=r.pm_outcome,
            outcome_side=r.outcome_side,
            sport=r.sport,
            league=r.league,
            pm_event_title=r.pm_event_title,
            action=r.action,
            reason=r.reason,
            fair_prob=r.fair_prob,
            poly_best_bid=r.poly_best_bid,
            poly_best_ask=r.poly_best_ask,
            poly_ask_depth_usd=r.poly_ask_depth_usd,
            pinnacle_decimal_odd=r.pinnacle_decimal_odd,
            ev=r.ev,
            proposed_stake_usd=r.proposed_stake_usd,
            proposed_price=r.proposed_price,
            seconds_to_kickoff=r.seconds_to_kickoff,
        )
        for r in rows
    ]
