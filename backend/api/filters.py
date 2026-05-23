"""Market filter endpoints — selected sports/leagues/events to monitor."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import require_auth
from backend.db import get_session
from backend.models import MarketFilter
from backend.polymarket.tree import (
    cache_age_seconds,
    load_cached_tree,
    refresh_tree,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/filters", tags=["filters"])

FilterLevel = Literal["sport", "league", "event"]


class FilterRef(BaseModel):
    level: FilterLevel
    identifier: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)


class FiltersResponse(BaseModel):
    tree: dict | None
    tree_age_seconds: float | None
    tree_event_count: int
    selected: list[FilterRef]


class FiltersUpdateBody(BaseModel):
    selected: list[FilterRef]


@router.get("", response_model=FiltersResponse)
async def get_filters(
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> FiltersResponse:
    tree = await load_cached_tree(session)
    age = await cache_age_seconds(session)
    selected_rows = (
        await session.execute(select(MarketFilter).order_by(MarketFilter.level, MarketFilter.display_name))
    ).scalars().all()
    selected = [
        FilterRef(level=row.level, identifier=row.identifier, display_name=row.display_name)  # type: ignore[arg-type]
        for row in selected_rows
    ]
    return FiltersResponse(
        tree=tree,
        tree_age_seconds=age,
        tree_event_count=tree.get("total_events", 0) if tree else 0,
        selected=selected,
    )


@router.put("", response_model=FiltersResponse)
async def update_filters(
    body: FiltersUpdateBody,
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> FiltersResponse:
    # Replace-set semantics: delete all, re-insert from body. Simple and correct.
    await session.execute(delete(MarketFilter))
    for ref in body.selected:
        session.add(
            MarketFilter(
                level=ref.level,
                identifier=ref.identifier,
                display_name=ref.display_name,
            )
        )
    await session.commit()
    return await get_filters(_user=_user, session=session)


@router.post("/refresh-tree", response_model=FiltersResponse)
async def refresh_tree_endpoint(
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> FiltersResponse:
    try:
        await refresh_tree(session)
    except Exception as exc:
        logger.exception("Failed to refresh Polymarket tree")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not refresh tree: {exc}",
        ) from exc
    return await get_filters(_user=_user, session=session)
