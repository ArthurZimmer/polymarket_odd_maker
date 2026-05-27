"""History endpoints — aggregates over closed positions + CSV export."""
from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import require_auth
from backend.db import get_session
from backend.models import DecisionLog, Order, Position

router = APIRouter(prefix="/api/history", tags=["history"])


class HistoryPositionRow(BaseModel):
    id: int
    polymarket_event_id: str | None
    sport: str | None
    league: str | None
    pm_event_title: str | None
    outcome: str | None
    outcome_side: str | None
    size: float
    entry_price: float
    entry_at: str
    exit_price: float | None
    exit_at: str | None
    pnl_usd: float | None
    status: str
    ev_entry: float | None
    fair_prob_entry: float | None


class PnlDailyPoint(BaseModel):
    date: str        # ISO date "YYYY-MM-DD" (UTC)
    pnl_usd: float
    trades: int
    wins: int
    losses: int
    cumulative_pnl_usd: float


class HistorySummary(BaseModel):
    total_positions: int
    open_positions: int
    closed_positions: int
    total_pnl_usd: float
    realized_pnl_today_usd: float
    realized_pnl_7d_usd: float
    realized_pnl_30d_usd: float
    win_rate_pct: float | None
    avg_pnl_usd: float | None
    best_position_pnl_usd: float | None
    worst_position_pnl_usd: float | None


def _parse_iso_date(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        # Accept "YYYY-MM-DD" or full ISO.
        if len(v) == 10:
            return datetime.combine(date.fromisoformat(v), time.min, tzinfo=UTC)
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _start_of_today_utc() -> datetime:
    return datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)


async def _decision_lookup(
    session: AsyncSession, positions: list[Position]
) -> dict[int, DecisionLog]:
    """Best-effort: match each position to the DecisionLog that triggered
    its entry Order. Returns {position.id: DecisionLog} (only those found).
    """
    if not positions:
        return {}
    entry_order_ids = [p.entry_order_id for p in positions if p.entry_order_id is not None]
    if not entry_order_ids:
        return {}
    orders = (
        await session.execute(
            select(Order).where(Order.id.in_(entry_order_ids))
        )
    ).scalars().all()
    dec_ids = {o.decision_id for o in orders if o.decision_id is not None}
    if not dec_ids:
        return {}
    decisions = (
        await session.execute(
            select(DecisionLog).where(DecisionLog.id.in_(dec_ids))
        )
    ).scalars().all()
    dec_by_id = {d.id: d for d in decisions}
    order_to_dec: dict[int, DecisionLog] = {}
    for o in orders:
        if o.decision_id and o.decision_id in dec_by_id:
            order_to_dec[o.id] = dec_by_id[o.decision_id]
    return {
        p.id: order_to_dec[p.entry_order_id]
        for p in positions
        if p.entry_order_id is not None and p.entry_order_id in order_to_dec
    }


def _row_from(p: Position, d: DecisionLog | None) -> HistoryPositionRow:
    return HistoryPositionRow(
        id=p.id,
        polymarket_event_id=p.polymarket_event_id,
        sport=d.sport if d else None,
        league=d.league if d else None,
        pm_event_title=d.pm_event_title if d else None,
        outcome=p.outcome,
        outcome_side=d.outcome_side if d else None,
        size=p.size,
        entry_price=p.entry_price,
        entry_at=p.entry_at.isoformat() if p.entry_at else "",
        exit_price=p.exit_price,
        exit_at=p.exit_at.isoformat() if p.exit_at else None,
        pnl_usd=p.pnl_usd,
        status=p.status,
        ev_entry=d.ev if d else None,
        fair_prob_entry=d.fair_prob if d else None,
    )


@router.get("/positions", response_model=list[HistoryPositionRow])
async def history_positions(
    status: str | None = Query(None, description="OPEN|CLOSED|ALL (default ALL)"),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> list[HistoryPositionRow]:
    stmt = select(Position)
    if status and status.upper() in {"OPEN", "CLOSED"}:
        stmt = stmt.where(Position.status == status.upper())
    dt_from = _parse_iso_date(from_)
    dt_to = _parse_iso_date(to)
    if dt_from:
        stmt = stmt.where(Position.entry_at >= dt_from.replace(tzinfo=None))
    if dt_to:
        stmt = stmt.where(Position.entry_at <= dt_to.replace(tzinfo=None))
    stmt = stmt.order_by(desc(Position.id)).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    dec_by_pos = await _decision_lookup(session, list(rows))
    return [_row_from(p, dec_by_pos.get(p.id)) for p in rows]


@router.get("/pnl-daily", response_model=list[PnlDailyPoint])
async def pnl_daily(
    days: int = Query(30, ge=1, le=365),
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> list[PnlDailyPoint]:
    """Buckets realized PnL per UTC day for the last `days` days."""
    horizon = datetime.now(UTC) - timedelta(days=days)
    horizon_naive = horizon.replace(tzinfo=None)
    rows = (
        await session.execute(
            select(Position)
            .where(
                and_(
                    Position.status == "CLOSED",
                    Position.exit_at.is_not(None),
                    Position.exit_at >= horizon_naive,
                )
            )
            .order_by(Position.exit_at.asc())
        )
    ).scalars().all()
    by_day: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.exit_at is None:
            continue
        d = r.exit_at.date().isoformat()
        bucket = by_day.setdefault(d, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
        bucket["pnl"] += float(r.pnl_usd or 0.0)
        bucket["trades"] += 1
        if (r.pnl_usd or 0.0) > 0:
            bucket["wins"] += 1
        elif (r.pnl_usd or 0.0) < 0:
            bucket["losses"] += 1
    out: list[PnlDailyPoint] = []
    cumulative = 0.0
    for d in sorted(by_day.keys()):
        b = by_day[d]
        cumulative += b["pnl"]
        out.append(
            PnlDailyPoint(
                date=d,
                pnl_usd=round(b["pnl"], 4),
                trades=int(b["trades"]),
                wins=int(b["wins"]),
                losses=int(b["losses"]),
                cumulative_pnl_usd=round(cumulative, 4),
            )
        )
    return out


@router.get("/summary", response_model=HistorySummary)
async def summary(
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> HistorySummary:
    total_n = (
        await session.execute(select(func.count(Position.id)))
    ).scalar_one()
    open_n = (
        await session.execute(
            select(func.count(Position.id)).where(Position.status == "OPEN")
        )
    ).scalar_one()
    closed_n = (
        await session.execute(
            select(func.count(Position.id)).where(Position.status == "CLOSED")
        )
    ).scalar_one()
    total_pnl = (
        await session.execute(
            select(func.coalesce(func.sum(Position.pnl_usd), 0.0))
        )
    ).scalar_one()
    start_today = _start_of_today_utc().replace(tzinfo=None)
    start_7d = (datetime.now(UTC) - timedelta(days=7)).replace(tzinfo=None)
    start_30d = (datetime.now(UTC) - timedelta(days=30)).replace(tzinfo=None)
    pnl_today = (
        await session.execute(
            select(func.coalesce(func.sum(Position.pnl_usd), 0.0)).where(
                and_(Position.status == "CLOSED", Position.exit_at >= start_today)
            )
        )
    ).scalar_one()
    pnl_7d = (
        await session.execute(
            select(func.coalesce(func.sum(Position.pnl_usd), 0.0)).where(
                and_(Position.status == "CLOSED", Position.exit_at >= start_7d)
            )
        )
    ).scalar_one()
    pnl_30d = (
        await session.execute(
            select(func.coalesce(func.sum(Position.pnl_usd), 0.0)).where(
                and_(Position.status == "CLOSED", Position.exit_at >= start_30d)
            )
        )
    ).scalar_one()
    wins = (
        await session.execute(
            select(func.count(Position.id)).where(
                and_(Position.status == "CLOSED", Position.pnl_usd > 0)
            )
        )
    ).scalar_one()
    best = (
        await session.execute(
            select(func.max(Position.pnl_usd)).where(Position.status == "CLOSED")
        )
    ).scalar_one()
    worst = (
        await session.execute(
            select(func.min(Position.pnl_usd)).where(Position.status == "CLOSED")
        )
    ).scalar_one()

    win_rate = (wins / closed_n * 100.0) if closed_n else None
    avg = (total_pnl / closed_n) if closed_n else None
    return HistorySummary(
        total_positions=int(total_n),
        open_positions=int(open_n),
        closed_positions=int(closed_n),
        total_pnl_usd=round(float(total_pnl or 0.0), 4),
        realized_pnl_today_usd=round(float(pnl_today or 0.0), 4),
        realized_pnl_7d_usd=round(float(pnl_7d or 0.0), 4),
        realized_pnl_30d_usd=round(float(pnl_30d or 0.0), 4),
        win_rate_pct=round(win_rate, 2) if win_rate is not None else None,
        avg_pnl_usd=round(float(avg), 4) if avg is not None else None,
        best_position_pnl_usd=round(float(best), 4) if best is not None else None,
        worst_position_pnl_usd=round(float(worst), 4) if worst is not None else None,
    )


@router.get("/export.csv")
async def export_csv(
    status: str | None = Query(None, description="OPEN|CLOSED|ALL"),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream CSV of positions with decision-context columns joined in."""
    stmt = select(Position)
    if status and status.upper() in {"OPEN", "CLOSED"}:
        stmt = stmt.where(Position.status == status.upper())
    dt_from = _parse_iso_date(from_)
    dt_to = _parse_iso_date(to)
    if dt_from:
        stmt = stmt.where(Position.entry_at >= dt_from.replace(tzinfo=None))
    if dt_to:
        stmt = stmt.where(Position.entry_at <= dt_to.replace(tzinfo=None))
    stmt = stmt.order_by(desc(Position.id))
    rows = (await session.execute(stmt)).scalars().all()
    dec_by_pos = await _decision_lookup(session, list(rows))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "position_id", "status", "sport", "league", "pm_event_title",
        "outcome", "outcome_side", "size", "entry_price", "entry_at",
        "exit_price", "exit_at", "pnl_usd", "ev_entry", "fair_prob_entry",
        "polymarket_event_id", "token_id",
    ])
    for p in rows:
        d = dec_by_pos.get(p.id)
        writer.writerow([
            p.id,
            p.status,
            d.sport if d else "",
            d.league if d else "",
            d.pm_event_title if d else "",
            p.outcome or "",
            d.outcome_side if d else "",
            f"{p.size:.4f}",
            f"{p.entry_price:.6f}",
            p.entry_at.isoformat() if p.entry_at else "",
            f"{p.exit_price:.6f}" if p.exit_price is not None else "",
            p.exit_at.isoformat() if p.exit_at else "",
            f"{p.pnl_usd:.4f}" if p.pnl_usd is not None else "",
            f"{d.ev:.6f}" if (d and d.ev is not None) else "",
            f"{d.fair_prob:.6f}" if (d and d.fair_prob is not None) else "",
            p.polymarket_event_id or "",
            p.token_id,
        ])
    buf.seek(0)
    filename = f"poly-scraper-history-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
