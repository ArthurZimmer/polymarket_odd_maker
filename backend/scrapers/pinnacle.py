"""Pinnacle scraper — uses the public Guest API used by pinnacle.com itself.

The `x-api-key` value is the one Pinnacle's own web app uses (observable via
DevTools) and works without authentication. No proxies needed for now.

Per cycle the scraper:
  1. Fetches matchups + markets for each configured sport (one HTTP call each).
  2. Filters to primary moneyline markets (period=0, type=moneyline).
  3. Joins matchup → market by matchupId; extracts Home/Draw/Away decimal odds.
  4. Upserts ExternalEvent rows and publishes OddsSnapshots to the bus.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from curl_cffi import requests as curl_requests
from sqlalchemy import select

from backend.engine.odds_bus import OddsSnapshot
from backend.models import ExternalEvent
from backend.models import OddsSnapshot as OddsSnapshotRow
from backend.scrapers.base import BookmakerScraper

logger = logging.getLogger(__name__)

PINNACLE_API_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
PINNACLE_GUEST_API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
PINNACLE_HEADERS = {
    "x-api-key": PINNACLE_GUEST_API_KEY,
    "referer": "https://www.pinnacle.com/",
    "origin": "https://www.pinnacle.com",
    "accept": "application/json",
    "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Pinnacle sport_id -> our canonical sport id (must match polymarket.tree.KNOWN_SPORT_MAP values)
PINNACLE_SPORT_MAP: dict[int, str] = {
    29: "soccer",
    4: "nba",          # Basketball — pinnacle bucket includes NCAA + non-NBA, OK for V1
    12: "esports",
    33: "tennis",
    22: "mma",
    15: "nfl",         # American Football
    19: "nhl",         # Ice Hockey
    3: "mlb",          # Baseball
    6: "boxing",
    17: "golf",
    44: "f1",
    8: "cricket",
}


def american_to_decimal(american: float) -> float:
    if american >= 100:
        return 1.0 + (american / 100.0)
    if american <= -100:
        return 1.0 + (100.0 / abs(american))
    # Pinnacle sometimes uses values like 0 to mean "no line". Treat as invalid.
    raise ValueError(f"American odd out of range: {american}")


@dataclass(slots=True)
class _MatchupInfo:
    matchup_id: str
    sport: str
    league: str
    home: str
    away: str
    draw_allowed: bool
    start_time: datetime


def _participants_to_teams(participants: list[dict]) -> tuple[str | None, str | None, bool]:
    home: str | None = None
    away: str | None = None
    draw_allowed = False
    for p in participants:
        align = (p.get("alignment") or "").lower()
        name = (p.get("name") or "").strip()
        if not name:
            continue
        if align == "home":
            home = name
        elif align == "away":
            away = name
        elif align == "neutral":
            # In soccer-style markets the third "neutral" participant represents the draw.
            if name.lower() in {"draw", "tie", "x"}:
                draw_allowed = True
    return home, away, draw_allowed


class PinnacleScraper(BookmakerScraper):
    name = "pinnacle"

    def __init__(self, bus, session_factory, *, sport_ids: list[int] | None = None) -> None:
        super().__init__(bus, session_factory, base_interval_s=20.0, max_interval_s=600.0)
        self.sport_ids = sport_ids or [29, 4, 12, 33, 22, 19, 3, 6]
        self._session = curl_requests.Session(
            impersonate="chrome120",
            headers=PINNACLE_HEADERS,
            timeout=30,
        )

    async def _fetch_once(self) -> int:
        import asyncio

        total = 0
        for sport_id in self.sport_ids:
            try:
                fetched: list[tuple[_MatchupInfo, list[tuple[str, float]]]] = (
                    await asyncio.to_thread(self._fetch_sport_data_sync, sport_id)
                )
            except Exception:
                logger.exception("Pinnacle: sport_id=%d HTTP fetch failed", sport_id)
                continue
            if not fetched:
                continue
            try:
                total += await self._persist_sport_data(fetched)
            except Exception:
                logger.exception("Pinnacle: sport_id=%d persist failed", sport_id)
        return total

    def _fetch_sport_data_sync(
        self, sport_id: int
    ) -> list[tuple[_MatchupInfo, list[tuple[str, float]]]]:
        """HTTP + parse only (sync, runs in thread). No DB, no bus, no asyncio."""
        sport_canonical = PINNACLE_SPORT_MAP.get(sport_id, f"pinnacle:{sport_id}")
        matchups = self._get_json(f"/sports/{sport_id}/matchups")
        markets = self._get_json(f"/sports/{sport_id}/markets/straight?primaryOnly=true")

        info_by_id: dict[str, _MatchupInfo] = {}
        for m in matchups:
            if m.get("isLive") or m.get("type") not in (None, "matchup"):
                continue
            participants = m.get("participants") or []
            home, away, draw = _participants_to_teams(participants)
            if not home or not away:
                continue
            start_iso = m.get("startTime")
            if not start_iso:
                continue
            try:
                start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            mid = str(m.get("id"))
            league_name = (m.get("league") or {}).get("name") or ""
            info_by_id[mid] = _MatchupInfo(
                matchup_id=mid,
                sport=sport_canonical,
                league=league_name,
                home=home,
                away=away,
                draw_allowed=draw,
                start_time=start,
            )

        snapshots: list[tuple[_MatchupInfo, list[tuple[str, float]]]] = []
        for mkt in markets:
            if mkt.get("type") != "moneyline" or mkt.get("period") != 0:
                continue
            mid = str(mkt.get("matchupId"))
            info = info_by_id.get(mid)
            if info is None:
                continue
            prices = mkt.get("prices") or []
            outcomes = self._prices_to_outcomes(prices, info)
            if not outcomes:
                continue
            snapshots.append((info, outcomes))
        return snapshots

    async def _persist_sport_data(
        self,
        snapshots: list[tuple[_MatchupInfo, list[tuple[str, float]]]],
    ) -> int:
        n = 0
        async with self.session_factory() as session:
            now = datetime.now(UTC)
            for info, outcomes in snapshots:
                existing = (
                    await session.execute(
                        select(ExternalEvent).where(
                            ExternalEvent.source == "pinnacle",
                            ExternalEvent.source_event_id == info.matchup_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        ExternalEvent(
                            source="pinnacle",
                            source_event_id=info.matchup_id,
                            sport=info.sport,
                            league=info.league,
                            home_team=info.home,
                            away_team=info.away,
                            start_time=info.start_time,
                        )
                    )
                else:
                    existing.sport = info.sport
                    existing.league = info.league
                    existing.home_team = info.home
                    existing.away_team = info.away
                    existing.start_time = info.start_time
                for outcome, decimal_odd in outcomes:
                    snap = OddsSnapshot(
                        source="pinnacle",
                        event_id=f"pinnacle:{info.matchup_id}",
                        market_condition_id=None,
                        token_id=None,
                        outcome=outcome,
                        best_bid=None,
                        best_ask=decimal_odd,
                        bid_depth_usd=None,
                        ask_depth_usd=None,
                        captured_at=now,
                    )
                    await self.bus.publish(snap)
                    session.add(
                        OddsSnapshotRow(
                            source="pinnacle",
                            event_id=f"pinnacle:{info.matchup_id}",
                            market_condition_id=None,
                            token_id=None,
                            outcome=outcome,
                            best_bid=None,
                            best_ask=decimal_odd,
                            mid_price=decimal_odd,
                            bid_depth_usd=None,
                            ask_depth_usd=None,
                            captured_at=now,
                        )
                    )
                    n += 1
            await session.commit()
        return n

    def _prices_to_outcomes(
        self, prices: list[dict], info: _MatchupInfo
    ) -> list[tuple[str, float]]:
        # Prefer Pinnacle's `designation` field (home|away|draw) when present
        # — soccer markets always carry it and it's the only way to know the
        # draw odd, since the draw isn't a participant.
        out: list[tuple[str, float]] = []
        has_designation = any("designation" in p for p in prices)
        if has_designation:
            for p in prices:
                des = (p.get("designation") or "").lower()
                raw = p.get("price")
                if raw is None:
                    continue
                if des == "home":
                    label = info.home
                elif des == "away":
                    label = info.away
                elif des == "draw":
                    label = "Draw"
                else:
                    continue
                try:
                    dec = american_to_decimal(float(raw))
                except (TypeError, ValueError):
                    continue
                out.append((label, dec))
            return out
        # Fallback: order-based (2-way sports where designation may be absent).
        order: list[str]
        if info.draw_allowed and len(prices) >= 3:
            order = [info.home, "Draw", info.away]
        else:
            order = [info.home, info.away]
        if len(prices) < len(order):
            return out
        for outcome_name, price_obj in zip(order, prices):
            raw = price_obj.get("price")
            if raw is None:
                continue
            try:
                dec = american_to_decimal(float(raw))
            except (TypeError, ValueError):
                continue
            out.append((outcome_name, dec))
        return out

    def _get_json(self, path: str) -> list[dict[str, Any]]:
        url = PINNACLE_API_BASE + path
        resp = self._session.get(url)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Pinnacle {path} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        return data if isinstance(data, list) else []
