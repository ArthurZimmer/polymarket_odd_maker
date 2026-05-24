"""Owns the lifecycle of all enabled bookmaker scrapers."""
from __future__ import annotations

import asyncio
import logging

from backend.config import settings
from backend.engine.odds_bus import OddsBus
from backend.scrapers.aggregators.the_odds_api import TheOddsApiScraper
from backend.scrapers.base import BookmakerScraper
from backend.scrapers.pinnacle import PinnacleScraper

logger = logging.getLogger(__name__)


class ScrapingCoordinator:
    def __init__(self, bus: OddsBus, session_factory) -> None:
        self.bus = bus
        self.session_factory = session_factory
        self.scrapers: list[BookmakerScraper] = [
            PinnacleScraper(bus=bus, session_factory=session_factory),
            TheOddsApiScraper(
                bus=bus,
                session_factory=session_factory,
                api_key=settings.the_odds_api_key,
            ),
            # Etapa 8.5: BetanoScraper, EstrelaBetScraper, SuperbetScraper
        ]
        if not settings.the_odds_api_key:
            logger.info(
                "TheOddsApi scraper mounted but inert — set THE_ODDS_API_KEY in .env to enable"
            )
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for s in self.scrapers:
            self._tasks.append(asyncio.create_task(s.run(), name=f"scraper-{s.name}"))
        logger.info("ScrapingCoordinator started %d scrapers", len(self.scrapers))

    async def stop(self) -> None:
        for s in self.scrapers:
            s.stop()
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout=10)
        except asyncio.TimeoutError:
            for t in self._tasks:
                if not t.done():
                    t.cancel()
        self._tasks.clear()

    def stats(self) -> list[dict]:
        return [s.stats.to_dict() for s in self.scrapers]
