"""Bookmaker scrapers status endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.api.auth import require_auth

router = APIRouter(prefix="/api/scrapers", tags=["scrapers"])


class ScraperStatus(BaseModel):
    name: str
    health: str
    last_run_at: str | None
    last_success_at: str | None
    last_error: str | None
    consecutive_failures: int
    interval_s: float
    total_runs: int
    total_failures: int
    total_snapshots_published: int
    snapshots_last_run: int
    last_latency_ms: float | None
    runs_per_min: float


@router.get("/status", response_model=list[ScraperStatus])
async def status(
    request: Request,
    _user: str = Depends(require_auth),
) -> list[ScraperStatus]:
    coord = getattr(request.app.state, "scrapers", None)
    if coord is None:
        return []
    return [ScraperStatus(**s) for s in coord.stats()]
