"""Build the Polymarket sports tree (Sport → League → Event) from raw events.

Strategy:
  - Each event has a `tags` array. We pick the first known canonical sport tag
    as the "sport" bucket, the next non-meta tag as the "league" bucket, and
    the event itself goes at the leaf.
  - When an event has BOTH a specific (e.g. `nba`) and a generic (`basketball`)
    sport tag, we prefer the specific. The generic stays in the tag list and
    will be skipped naturally.

The discovered tree is cached in `polymarket_tree_cache` (single-row JSON blob).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import PolymarketTreeCache
from backend.polymarket.client import PolymarketGammaClient

logger = logging.getLogger(__name__)

# Canonical sport identifiers — keys are Polymarket tag slugs (lowercase).
KNOWN_SPORT_MAP: dict[str, tuple[str, str]] = {
    "soccer": ("soccer", "Soccer"),
    "football": ("soccer", "Soccer"),
    "nfl": ("nfl", "NFL"),
    "nba": ("nba", "NBA"),
    "mlb": ("mlb", "MLB"),
    "nhl": ("nhl", "NHL"),
    "tennis": ("tennis", "Tennis"),
    "mma": ("mma", "MMA"),
    "ufc": ("mma", "MMA"),
    "boxing": ("boxing", "Boxing"),
    "esports": ("esports", "eSports"),
    "golf": ("golf", "Golf"),
    "f1": ("f1", "Formula 1"),
    "formula-1": ("f1", "Formula 1"),
    "cricket": ("cricket", "Cricket"),
    "rugby": ("rugby", "Rugby"),
    "olympics": ("olympics", "Olympics"),
    "winter-games": ("winter-olympics", "Winter Olympics"),
    "darts": ("darts", "Darts"),
    "snooker": ("snooker", "Snooker"),
    "basketball": ("basketball-generic", "Basketball"),
    "hockey": ("hockey-generic", "Hockey"),
    "baseball": ("baseball-generic", "Baseball"),
}

# When both are present, prefer the specific one (left wins over right).
SPECIFIC_OVER_GENERIC: dict[str, str] = {
    "nba": "basketball",
    "nhl": "hockey",
    "mlb": "baseball",
}

META_TAG_SLUGS: set[str] = {
    "sports",
    "games",
    "hide-from-new",
    "trending",
    "featured",
    "new",
    "weekly",
    "all",
}


def _pick_sport(tags: list[dict[str, Any]]) -> tuple[str, str]:
    slugs = [t.get("slug", "").lower() for t in tags if t.get("slug")]
    # Drop generics if their specific counterpart is also present.
    drop: set[str] = set()
    for specific, generic in SPECIFIC_OVER_GENERIC.items():
        if specific in slugs and generic in slugs:
            drop.add(generic)
    for slug in slugs:
        if slug in drop:
            continue
        if slug in KNOWN_SPORT_MAP:
            return KNOWN_SPORT_MAP[slug]
    return ("other", "Other")


def _pick_league(
    tags: list[dict[str, Any]], sport_canonical_id: str
) -> tuple[str, str]:
    for tag in tags:
        slug = (tag.get("slug") or "").lower()
        label = (tag.get("label") or "").strip()
        if not slug or slug in META_TAG_SLUGS:
            continue
        if slug in KNOWN_SPORT_MAP and KNOWN_SPORT_MAP[slug][0] == sport_canonical_id:
            # Already used as the sport bucket
            continue
        return slug, label or slug
    return ("general", "General")


def build_tree(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Group raw events into nested {sports: [{leagues: [{events: [...]}]}]}."""
    sports: dict[str, dict[str, Any]] = {}
    for ev in events:
        tags = ev.get("tags") or []
        sport_id, sport_label = _pick_sport(tags)
        league_id, league_label = _pick_league(tags, sport_id)

        sport = sports.setdefault(
            sport_id, {"id": sport_id, "label": sport_label, "leagues": {}}
        )
        league = sport["leagues"].setdefault(
            league_id, {"id": league_id, "label": league_label, "events": []}
        )
        league["events"].append(
            {
                "id": str(ev.get("id", "")),
                "slug": ev.get("slug") or "",
                "title": (ev.get("title") or "").strip(),
                "start_date": ev.get("startDate"),
                "end_date": ev.get("endDate"),
                "market_count": len(ev.get("markets") or []),
                "volume_24h": ev.get("volume24hr") or 0,
                "liquidity": ev.get("liquidity") or 0,
            }
        )

    # Order: events by start_date asc; leagues by # events desc; sports by label asc.
    def _norm(sport: dict[str, Any]) -> dict[str, Any]:
        leagues_list: list[dict[str, Any]] = []
        for league in sport["leagues"].values():
            league["events"].sort(key=lambda e: e.get("start_date") or "")
            league["event_count"] = len(league["events"])
            leagues_list.append(league)
        leagues_list.sort(key=lambda lg: (-lg["event_count"], lg["label"]))
        return {
            "id": sport["id"],
            "label": sport["label"],
            "leagues": leagues_list,
            "event_count": sum(lg["event_count"] for lg in leagues_list),
        }

    sports_list = sorted(
        (_norm(s) for s in sports.values()), key=lambda s: (s["id"] == "other", s["label"])
    )
    return {"sports": sports_list, "total_events": sum(s["event_count"] for s in sports_list)}


async def refresh_tree(session: AsyncSession) -> dict[str, Any]:
    """Fetch fresh events from Polymarket, build tree, persist cache, return tree."""
    async with PolymarketGammaClient() as client:
        events = await client.fetch_sports_events()
    tree = build_tree(events)
    payload = json.dumps(tree, separators=(",", ":"), ensure_ascii=False)

    row = (await session.execute(select(PolymarketTreeCache).where(PolymarketTreeCache.id == 1))).scalar_one_or_none()
    if row is None:
        row = PolymarketTreeCache(id=1, payload=payload, event_count=tree["total_events"])
        session.add(row)
    else:
        row.payload = payload
        row.event_count = tree["total_events"]
        row.updated_at = datetime.now(UTC)
    await session.commit()
    logger.info("Polymarket tree refreshed: %d events, %d sports", tree["total_events"], len(tree["sports"]))
    return tree


async def load_cached_tree(session: AsyncSession) -> dict[str, Any] | None:
    row = (await session.execute(select(PolymarketTreeCache).where(PolymarketTreeCache.id == 1))).scalar_one_or_none()
    if row is None:
        return None
    try:
        return json.loads(row.payload)
    except json.JSONDecodeError:
        return None


async def cache_age_seconds(session: AsyncSession) -> float | None:
    row = (await session.execute(select(PolymarketTreeCache).where(PolymarketTreeCache.id == 1))).scalar_one_or_none()
    if row is None:
        return None
    delta = datetime.now(UTC) - row.updated_at.replace(tzinfo=UTC)
    return delta.total_seconds()
