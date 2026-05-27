"""SQLAlchemy async engine + session factory.

Models live in backend.models. Migrations under alembic/.

SQLite tuning: we enable WAL (write-ahead logging) on every new connection so
that the Watcher's high-frequency writes don't block ad-hoc readers (sqlite3
CLI, the API endpoints, etc.).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.db_url, future=True, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragma_on_connect(dbapi_conn, _connection_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    # FULL is slower than NORMAL but guarantees durability against OS crashes.
    # Acceptable trade-off for a single-user bot trading real USDC: a few extra
    # milliseconds per commit beats losing the record of a trade we just sent
    # to the CLOB.
    cursor.execute("PRAGMA synchronous=FULL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
