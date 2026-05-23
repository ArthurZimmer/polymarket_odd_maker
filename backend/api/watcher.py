"""Polymarket watcher status endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.api.auth import require_auth

router = APIRouter(prefix="/api/watcher", tags=["watcher"])


class WatcherStatus(BaseModel):
    connected: bool
    connected_at: str | None
    last_disconnect_at: str | None
    last_disconnect_reason: str | None
    subscribed_tokens: int
    subscribed_events: int
    subscription_truncated: bool
    total_messages: int
    updates_per_min: float
    last_message_at: str | None


@router.get("/status", response_model=WatcherStatus)
async def status(
    request: Request,
    _user: str = Depends(require_auth),
) -> WatcherStatus:
    watcher = getattr(request.app.state, "watcher", None)
    if watcher is None:
        return WatcherStatus(
            connected=False,
            connected_at=None,
            last_disconnect_at=None,
            last_disconnect_reason="watcher_not_started",
            subscribed_tokens=0,
            subscribed_events=0,
            subscription_truncated=False,
            total_messages=0,
            updates_per_min=0.0,
            last_message_at=None,
        )
    return WatcherStatus(**watcher.stats.to_dict())
