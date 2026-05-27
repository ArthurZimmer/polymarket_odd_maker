"""Bookmaker scrapers + proxy pool status endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.api.auth import require_auth
from backend.scrapers.proxies import proxy_pool

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
    last_proxy: str | None = None
    last_blocked_at: str | None = None
    block_count: int = 0
    network_errors: int = 0


@router.get("/status", response_model=list[ScraperStatus])
async def status(
    request: Request,
    _user: str = Depends(require_auth),
) -> list[ScraperStatus]:
    coord = getattr(request.app.state, "scrapers", None)
    if coord is None:
        return []
    return [ScraperStatus(**s) for s in coord.stats()]


@router.get("/proxies")
async def proxies(
    _user: str = Depends(require_auth),
) -> dict:
    """Pool snapshot — counts, threshold, cooldown window, per-proxy state."""
    return proxy_pool.stats()
