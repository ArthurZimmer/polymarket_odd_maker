"""The Odds API scraper — single endpoint per sport returns events with odds
from many bookmakers at once.

Why this scraper exists
-----------------------
Etapa 8 wants ≥3 traditional books in the consensus. Direct scrapers for
bet365/Betano/Superbet are expensive (anti-bot, region locks). The Odds API
($30/mo for 20k requests) covers all of them via a single REST endpoint.

Budget arithmetic
-----------------
20k req/month → ~28 req/h. With ~10 sports and a 600s interval we burn
~60 req/h. That blows the budget; the safer baseline is the 6 highest-volume
sports at a 600s interval ⇒ 36 req/h ⇒ ~864 req/day ⇒ ~26k req/month (still
hot — tune via the `sport_keys` ctor arg or extend `base_interval_s`).

Event/source model
------------------
- One `ExternalEvent` per match (source="the_odds_api", source_event_id=API id).
- One `OddsSnapshot` per (book, outcome side) — `source` is the book name
  (e.g. "bet365"), `event_id` is `the_odds_api:{id}`. This way the consensus
  weights in `engine/ev.py` (Pinnacle 0.40, bet365 0.20, …) work per-book
  unchanged.

The scraper no-ops without an API key — code stays mounted but harmless.
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

API_BASE = "https://api.the-odds-api.com/v4"
SOURCE_NAME = "the_odds_api"

# Sport keys → canonical sport id (must align with values used elsewhere).
# Trim/extend to manage quota. Soccer leagues collapse to one canonical id so
# the matcher can find them via _SPORT_BRIDGES["soccer"] → {"soccer"}.
DEFAULT_SPORT_KEYS: dict[str, str] = {
    "soccer_epl": "soccer",
    "soccer_uefa_champs_league": "soccer",
    "soccer_brazil_campeonato": "soccer",
    "soccer_spain_la_liga": "soccer",
    "soccer_italy_serie_a": "soccer",
    "basketball_nba": "nba",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "mma_mixed_martial_arts": "mma",
}

# Books we care about — others get dropped at parse time.
BOOK_WHITELIST: set[str] = {
    "bet365",
    "betano",
    "pinnacle",   # duplicate vs PinnacleScraper — useful as a cross-check
    "superbet",
    "betfair_ex_uk",
    "matchbook",
}


@dataclass(slots=True)
class _ParsedEvent:
    """One match across all whitelisted books. Outcomes are per-book.

    `book_outcomes[book_key] = [(outcome_name, decimal_odd), ...]`
    """

    api_event_id: str
    sport_canonical: str
    league_title: str
    home_team: str
    away_team: str
    commence_time: datetime
    book_outcomes: dict[str, list[tuple[str, float]]]


class TheOddsApiScraper(BookmakerScraper):
    name = "the_odds_api"

    def __init__(
        self,
        bus,
        session_factory,
        *,
        api_key: str | None = None,
        sport_keys: dict[str, str] | None = None,
        base_interval_s: float = 600.0,
    ) -> None:
        super().__init__(
            bus,
            session_factory,
            base_interval_s=base_interval_s,
            max_interval_s=3600.0,
        )
        self.api_key = api_key
        self.sport_keys = sport_keys or DEFAULT_SPORT_KEYS
        # Quota tracking — populated from response headers.
        self.quota_used: int | None = None
        self.quota_remaining: int | None = None
        self._session = curl_requests.Session(
            impersonate="chrome120",
            headers={"accept": "application/json"},
            timeout=30,
        )

    async def _fetch_once(self) -> int:
        if not self.api_key:
            # Stay alive but silent. Lets us mount the scraper in dev without
            # spamming the stats with errors.
            return 0
        import asyncio

        total = 0
        for sport_key, canonical in self.sport_keys.items():
            try:
                parsed = await asyncio.to_thread(
                    self._fetch_sport_sync, sport_key, canonical
                )
            except Exception:
                logger.exception("TheOddsApi sport=%s failed", sport_key)
                continue
            if not parsed:
                continue
            try:
                total += await self._persist_events(parsed)
            except Exception:
                logger.exception("TheOddsApi sport=%s persist failed", sport_key)
        return total

    def _fetch_sport_sync(self, sport_key: str, canonical: str) -> list[_ParsedEvent]:
        from backend.scrapers.proxies import proxied_get_sync

        url = (
            f"{API_BASE}/sports/{sport_key}/odds"
            f"?apiKey={self.api_key}"
            "&regions=us,uk,eu"
            "&markets=h2h"
            "&oddsFormat=decimal"
            "&dateFormat=iso"
        )
        resp = proxied_get_sync(self._session, url, stats=self.stats)
        # Update quota from response headers (header names per API docs).
        try:
            self.quota_used = int(resp.headers.get("x-requests-used") or 0) or self.quota_used
            self.quota_remaining = (
                int(resp.headers.get("x-requests-remaining") or 0) or self.quota_remaining
            )
        except (TypeError, ValueError):
            pass
        if resp.status_code == 401:
            raise RuntimeError("TheOddsApi: 401 — bad API key")
        if resp.status_code == 429:
            raise RuntimeError("TheOddsApi: 429 — quota exhausted")
        if resp.status_code != 200:
            raise RuntimeError(
                f"TheOddsApi {sport_key} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        events = resp.json()
        if not isinstance(events, list):
            return []
        return list(self._parse_events(events, canonical))

    def _parse_events(
        self, raw_events: list[dict[str, Any]], canonical: str
    ) -> list[_ParsedEvent]:
        out: list[_ParsedEvent] = []
        for ev in raw_events:
            api_id = ev.get("id")
            if not api_id:
                continue
            home = (ev.get("home_team") or "").strip()
            away = (ev.get("away_team") or "").strip()
            if not home or not away:
                continue
            commence = ev.get("commence_time")
            if not commence:
                continue
            try:
                commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            except ValueError:
                continue
            league = ev.get("sport_title") or ev.get("sport_key") or ""

            book_outcomes: dict[str, list[tuple[str, float]]] = {}
            for bk in ev.get("bookmakers") or []:
                book_key = bk.get("key")
                if not book_key or book_key not in BOOK_WHITELIST:
                    continue
                # The API gives a `markets` list — we only want h2h here.
                for market in bk.get("markets") or []:
                    if market.get("key") != "h2h":
                        continue
                    outcomes: list[tuple[str, float]] = []
                    for o in market.get("outcomes") or []:
                        name = (o.get("name") or "").strip()
                        price = o.get("price")
                        if not name or price is None:
                            continue
                        try:
                            decimal_odd = float(price)
                        except (TypeError, ValueError):
                            continue
                        if decimal_odd <= 1.0:
                            continue
                        outcomes.append((name, decimal_odd))
                    if outcomes:
                        book_outcomes[book_key] = outcomes
                    break  # one h2h market per book
            if not book_outcomes:
                continue
            out.append(
                _ParsedEvent(
                    api_event_id=api_id,
                    sport_canonical=canonical,
                    league_title=league,
                    home_team=home,
                    away_team=away,
                    commence_time=commence_dt,
                    book_outcomes=book_outcomes,
                )
            )
        return out

    async def _persist_events(self, parsed: list[_ParsedEvent]) -> int:
        if not parsed:
            return 0
        n = 0
        async with self.session_factory() as session:
            now = datetime.now(UTC)
            for ev in parsed:
                # Upsert the aggregator-level external_event.
                existing = (
                    await session.execute(
                        select(ExternalEvent).where(
                            ExternalEvent.source == SOURCE_NAME,
                            ExternalEvent.source_event_id == ev.api_event_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        ExternalEvent(
                            source=SOURCE_NAME,
                            source_event_id=ev.api_event_id,
                            sport=ev.sport_canonical,
                            league=ev.league_title,
                            home_team=ev.home_team,
                            away_team=ev.away_team,
                            start_time=ev.commence_time,
                        )
                    )
                else:
                    existing.sport = ev.sport_canonical
                    existing.league = ev.league_title
                    existing.home_team = ev.home_team
                    existing.away_team = ev.away_team
                    existing.start_time = ev.commence_time

                # One snapshot per (book, outcome).
                event_id_str = f"{SOURCE_NAME}:{ev.api_event_id}"
                for book_key, outcomes in ev.book_outcomes.items():
                    for outcome_name, decimal_odd in outcomes:
                        snap = OddsSnapshot(
                            source=book_key,
                            event_id=event_id_str,
                            market_condition_id=None,
                            token_id=None,
                            outcome=outcome_name,
                            best_bid=None,
                            best_ask=decimal_odd,
                            bid_depth_usd=None,
                            ask_depth_usd=None,
                            captured_at=now,
                        )
                        await self.bus.publish(snap)
                        session.add(
                            OddsSnapshotRow(
                                source=book_key,
                                event_id=event_id_str,
                                market_condition_id=None,
                                token_id=None,
                                outcome=outcome_name,
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
