"""FastAPI entrypoint.

Lifespan owns the long-running asyncio tasks:
  - PolymarketWatcher (WS realtime)
  - ScrapingCoordinator (Pinnacle today; more in Etapa 8)
  - EventMatcher (links PM events to external_events)
  - DecisionEngine (dry-run EV evaluator)
Trading engine, Position manager land in later etapas.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import auth as auth_routes
from backend.api import decisions as decisions_routes
from backend.api import filters as filters_routes
from backend.api import matcher as matcher_routes
from backend.api import scrapers as scrapers_routes
from backend.api import wallet as wallet_routes
from backend.api import watcher as watcher_routes
from backend.config import settings
from backend.db import SessionLocal
from backend.engine.decision import DecisionEngine
from backend.engine.odds_bus import OddsBus
from backend.matcher.matcher import EventMatcher
from backend.polymarket.watcher import PolymarketWatcher
from backend.scrapers.coordinator import ScrapingCoordinator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting poly-scraper backend on %s:%s", settings.host, settings.port)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    bus = OddsBus()
    watcher = PolymarketWatcher(bus=bus, session_factory=SessionLocal)
    coordinator = ScrapingCoordinator(bus=bus, session_factory=SessionLocal)
    matcher = EventMatcher(session_factory=SessionLocal)
    decision_engine = DecisionEngine(session_factory=SessionLocal, dry_run=True)
    app.state.bus = bus
    app.state.watcher = watcher
    app.state.scrapers = coordinator
    app.state.matcher = matcher
    app.state.decision_engine = decision_engine
    watcher_task = asyncio.create_task(watcher.run(), name="polymarket-watcher")
    matcher_task = asyncio.create_task(matcher.run(), name="event-matcher")
    decision_task = asyncio.create_task(decision_engine.run(), name="decision-engine")
    coordinator.start()

    try:
        yield
    finally:
        logger.info("Stopping watcher + scrapers + matcher + engine...")
        watcher.stop()
        matcher.stop()
        decision_engine.stop()
        await coordinator.stop()
        for name, task in (
            ("watcher", watcher_task),
            ("matcher", matcher_task),
            ("decision", decision_task),
        ):
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("%s did not stop in time; cancelling", name)
                task.cancel()
            except Exception:
                logger.exception("%s exited with error", name)
        logger.info("poly-scraper backend stopped")


app = FastAPI(title="poly-scraper", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "poly-scraper", "version": "0.1.0"}


app.include_router(auth_routes.router)
app.include_router(wallet_routes.router)
app.include_router(filters_routes.router)
app.include_router(watcher_routes.router)
app.include_router(scrapers_routes.router)
app.include_router(matcher_routes.router)
app.include_router(decisions_routes.router)
