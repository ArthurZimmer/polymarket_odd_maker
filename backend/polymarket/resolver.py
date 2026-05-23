"""Resolve user-selected MarketFilters into a concrete set of (token_id, ...) specs.

Strategy:
  1. Load filters from DB.
  2. Fetch fresh sports events from Gamma API (canonical source of truth for tokens).
  3. For each event, check if any of its tags/id matches a sport/league/event filter.
  4. For matching events, extract token_ids from event.markets[].clobTokenIds and
     pair each with its outcome label (event.markets[].outcomes).

Capped at MAX_TOKENS to fit one Polymarket WebSocket connection (~500 tokens).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import MarketFilter
from backend.polymarket.client import PolymarketGammaClient
from backend.polymarket.tree import _pick_sport

logger = logging.getLogger(__name__)

MAX_TOKENS = 500


@dataclass(frozen=True, slots=True)
class TokenSpec:
    token_id: str
    event_id: str
    event_title: str
    market_condition_id: str | None
    outcome: str
    event_start_iso: str | None


@dataclass(slots=True)
class SubscriptionPlan:
    tokens: list[TokenSpec]
    event_count: int
    truncated: bool


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _event_matches(event: dict, sports: set[str], leagues: set[str], event_ids: set[str]) -> bool:
    ev_id = str(event.get("id", ""))
    if ev_id in event_ids:
        return True
    tags = event.get("tags") or []
    if sports:
        sport_id, _ = _pick_sport(tags)
        if sport_id in sports:
            return True
    if leagues:
        for t in tags:
            slug = (t.get("slug") or "").lower()
            if slug in leagues:
                return True
    return False


async def resolve_subscriptions(session: AsyncSession) -> SubscriptionPlan:
    rows = (await session.execute(select(MarketFilter))).scalars().all()
    if not rows:
        return SubscriptionPlan(tokens=[], event_count=0, truncated=False)

    sports = {f.identifier for f in rows if f.level == "sport"}
    leagues = {f.identifier for f in rows if f.level == "league"}
    event_ids = {f.identifier for f in rows if f.level == "event"}

    async with PolymarketGammaClient() as client:
        events = await client.fetch_sports_events()

    tokens: list[TokenSpec] = []
    matched_events = 0
    truncated = False

    for ev in events:
        if not _event_matches(ev, sports, leagues, event_ids):
            continue
        matched_events += 1
        ev_id = str(ev.get("id", ""))
        ev_title = (ev.get("title") or "").strip()
        ev_start = ev.get("startDate")
        for m in ev.get("markets") or []:
            tids = _safe_json_list(m.get("clobTokenIds"))
            outcomes = _safe_json_list(m.get("outcomes")) or ["Yes", "No"]
            for i, tid in enumerate(tids):
                if not tid:
                    continue
                outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                tokens.append(
                    TokenSpec(
                        token_id=str(tid),
                        event_id=ev_id,
                        event_title=ev_title,
                        market_condition_id=m.get("conditionId"),
                        outcome=str(outcome),
                        event_start_iso=ev_start,
                    )
                )
                if len(tokens) >= MAX_TOKENS:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    if truncated:
        logger.warning(
            "Subscription plan truncated at %d tokens (matched %d+ events)", MAX_TOKENS, matched_events
        )

    return SubscriptionPlan(tokens=tokens, event_count=matched_events, truncated=truncated)
