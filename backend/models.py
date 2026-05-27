"""SQLAlchemy ORM models.

Cada tabela aqui é descoberta pelo Alembic via Base.metadata (ver alembic/env.py).
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


# Centralized order-state semantics — imported by trading.py, position_manager.py,
# risk.py, etc. PARTIAL is an in-flight order with some fills but not done yet.
ORDER_NON_TERMINAL: frozenset[str] = frozenset(
    {"PENDING_SUBMIT", "SUBMITTED", "PARTIAL"}
)
ORDER_TERMINAL: frozenset[str] = frozenset({"FILLED", "CANCELLED", "FAILED"})


# Exit-policy semantics for SELL orders created by the PositionManager.
EXIT_POLICY_RIDE = "ride"      # don't cancel on stale; let it ride to event resolution
EXIT_POLICY_CANCEL = "cancel"  # cancel after ORDER_STALE_TIMEOUT_S and let manager retry


class WalletConfig(Base):
    __tablename__ = "wallet_config"

    # Single-row table — id is always 1.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_private_key: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_api_key: Mapped[str | None] = mapped_column(String, nullable=True)
    encrypted_api_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    funder_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (CheckConstraint("id = 1", name="wallet_config_single_row"),)


class MarketFilter(Base):
    """User-selected filters from the Polymarket sports tree.

    A filter at any level acts as a *include* rule. Bot monitors an event when
    it matches at least one enabled filter (by sport, league, or event id).
    """

    __tablename__ = "market_filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)  # 'sport'|'league'|'event'
    identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("level", "identifier", name="uq_filter_level_identifier"),
        CheckConstraint(
            "level IN ('sport','league','event')", name="ck_filter_level_valid"
        ),
    )


class PolymarketTreeCache(Base):
    """Cached snapshot of the discovered Polymarket sports tree.

    Single-row table: id always = 1. `payload` holds the full tree as JSON
    (sports → leagues → events). Refresh logic owns its own TTL.
    """

    __tablename__ = "polymarket_tree_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="polymarket_tree_cache_single_row"),
    )


class OddsSnapshot(Base):
    """One row per orderbook update received from a source (Polymarket WS or a
    bookmaker scrape). Append-only; old rows are pruned by TTL job (V2).
    """

    __tablename__ = "odds_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # 'polymarket'|'pinnacle'|...
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_condition_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_depth_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_depth_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        Index("idx_odds_token_time", "token_id", "captured_at"),
        Index("idx_odds_event_time", "event_id", "captured_at"),
    )


class EventMatch(Base):
    """Resolved link between a Polymarket event and an ExternalEvent row.

    The matcher writes the best candidate per (polymarket_event_id, source).
    Score components are kept individually so we can audit a bad match later.
    """

    __tablename__ = "event_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    polymarket_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    home_score: Mapped[float] = mapped_column(Float, nullable=False)
    away_score: Mapped[float] = mapped_column(Float, nullable=False)
    time_delta_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "polymarket_event_id", "source", name="uq_match_polyev_source"
        ),
        Index("idx_match_polyev", "polymarket_event_id"),
        Index("idx_match_extev", "external_event_id"),
    )


class ExternalEvent(Base):
    """An event as seen by an external odds source (Pinnacle, Betano, the-odds-api, ...).

    The matcher (Etapa 6) joins these to Polymarket events via normalized team
    names + league + start_time fuzzy match.
    """

    __tablename__ = "external_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sport: Mapped[str | None] = mapped_column(String(64), nullable=True)
    league: Mapped[str | None] = mapped_column(String(128), nullable=True)
    home_team: Mapped[str] = mapped_column(String(128), nullable=False)
    away_team: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source", "source_event_id", name="uq_extev_source_id"),
        Index("idx_extev_source_start", "source", "start_time"),
        Index("idx_extev_teams_start", "home_team", "away_team", "start_time"),
    )


class PolymarketToken(Base):
    """One CLOB token (asset_id) per (event, outcome). Persisted so the EV
    engine can look up which side a token represents without going through
    the watcher's in-memory index.
    """

    __tablename__ = "polymarket_tokens"

    token_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    polymarket_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_condition_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(128), nullable=False)
    # Heuristic: which side of the matched external event this PM outcome
    # represents — 'home' | 'away' | 'draw' — or NULL until inferred.
    outcome_side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        Index("idx_pmtoken_event", "polymarket_event_id"),
    )


class DecisionLog(Base):
    """Every EV evaluation the engine performs, regardless of outcome.

    Logged in dry-run mode too — this is the audit trail and the data source
    for the realtime DecisionFeed in the dashboard. Keep schema flat so the
    UI doesn't need joins.
    """

    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )
    polymarket_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    polymarket_token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pm_outcome: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome_side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    sport: Mapped[str | None] = mapped_column(String(32), nullable=True)
    league: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pm_event_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Decision outcome: BUY | PASS_LOW_EV | PASS_WINDOW | PASS_LIQUIDITY |
    # PASS_NO_MATCH | PASS_NO_POLY_SNAP | PASS_NO_EXT_SNAP | PASS_DEVIG_FAILED
    # | ERROR
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Inputs (snapshot at decision time)
    fair_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    poly_best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    poly_best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    poly_ask_depth_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pinnacle_decimal_odd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pinnacle_raw_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Outputs (would-be order, dry-run)
    ev: Mapped[float | None] = mapped_column(Float, nullable=True)
    proposed_stake_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    proposed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    seconds_to_kickoff: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("idx_dlog_ts", "captured_at"),
        Index("idx_dlog_event", "polymarket_event_id"),
        Index("idx_dlog_action", "action", "captured_at"),
    )


class BotState(Base):
    """Single-row global control panel for the live bot.

    `is_running` is the master switch: when False the TradingEngine sees
    decisions but never sends an order. Risk knobs live here too — the
    DecisionEngine and RiskManager read them every cycle.
    """

    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    is_running: Mapped[bool] = mapped_column(default=False, nullable=False)
    master_stake_usd: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    ev_threshold: Mapped[float] = mapped_column(Float, default=0.03, nullable=False)
    exit_threshold: Mapped[float] = mapped_column(Float, default=0.005, nullable=False)
    max_concurrent_positions: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    max_daily_drawdown_usd: Mapped[float] = mapped_column(
        Float, default=100.0, nullable=False
    )
    stop_loss_pct: Mapped[float] = mapped_column(
        Float, default=0.30, nullable=False
    )
    max_total_exposure_usd: Mapped[float] = mapped_column(
        Float, default=200.0, nullable=False
    )
    last_pause_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    min_time_to_game_minutes: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    max_time_to_game_minutes: Mapped[int] = mapped_column(
        Integer, default=120, nullable=False
    )
    min_ask_depth_usd: Mapped[float] = mapped_column(
        Float, default=100.0, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (CheckConstraint("id = 1", name="bot_state_single_row"),)


class Order(Base):
    """A LIMIT order the bot tried to place on Polymarket.

    Status lifecycle:
        PENDING_SUBMIT → SUBMITTED → (FILLED | PARTIAL | CANCELLED | FAILED)
    `polymarket_order_id` is null until the CLOB ACKs the submission. Use
    `last_error` to record failed submits without losing the row.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    polymarket_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    polymarket_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(128), nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY|SELL
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)  # shares
    notional_usd: Mapped[float] = mapped_column(Float, nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), default="GTC", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    filled_size: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    filled_avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # SELL-only: 'ride' (don't cancel on stale) or 'cancel' (cancel + retry).
    # NULL for BUYs (no exit policy concept).
    exit_policy: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("polymarket_order_id", name="uq_orders_pm_id"),
        Index("idx_orders_status_ts", "status", "created_at"),
        Index("idx_orders_token", "token_id"),
    )


class Position(Base):
    """A filled BUY producing a long position on one Polymarket token.

    Etapa 10 will set `exit_*` and `pnl_usd` when the PositionManager closes
    out. For now we only ever create OPEN positions; closing is a stub.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    polymarket_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # PnL realized through partial-fill cancels before the position closes
    # fully. Final pnl_usd at CLOSED time = sum(partial_realized) + last close.
    realized_pnl_partial_usd: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    entry_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("idx_positions_status", "status"),
        Index("idx_positions_token", "token_id"),
        Index("idx_positions_exit_at", "exit_at"),
    )
