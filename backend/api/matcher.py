"""EventMatcher status endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.api.auth import require_auth

router = APIRouter(prefix="/api/matcher", tags=["matcher"])


class SportCoverage(BaseModel):
    parseable: int
    matchable: int
    matched: int


class MatcherStatus(BaseModel):
    last_run_at: str | None
    last_run_duration_ms: float | None
    total_runs: int
    last_pm_events_scanned: int
    last_pm_events_parseable: int
    last_pm_events_matchable: int
    last_matches_written: int
    last_matches_total: int
    coverage_pct: float
    coverage_by_sport: dict[str, SportCoverage]


@router.get("/status", response_model=MatcherStatus)
async def status(
    request: Request,
    _user: str = Depends(require_auth),
) -> MatcherStatus:
    matcher = getattr(request.app.state, "matcher", None)
    if matcher is None:
        return MatcherStatus(
            last_run_at=None,
            last_run_duration_ms=None,
            total_runs=0,
            last_pm_events_scanned=0,
            last_pm_events_parseable=0,
            last_pm_events_matchable=0,
            last_matches_written=0,
            last_matches_total=0,
            coverage_pct=0.0,
            coverage_by_sport={},
        )
    return MatcherStatus(**matcher.stats.to_dict())
