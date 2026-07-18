from __future__ import annotations

import os
from datetime import date, datetime

from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    """Kite instruments master — refreshed daily from api.kite.trade/instruments."""

    __tablename__ = "instruments"

    instrument_token: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange_token: Mapped[int] = mapped_column(Integer, nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)


class Alert(Base):
    """Inbound TradingView webhook — one row per unique signal."""

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_alerts_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tv_ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    tv_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)   # BUY / SELL / EXIT / TRAIL
    order_type: Mapped[str | None] = mapped_column(String(16))
    entry_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    sl_percent: Mapped[float | None] = mapped_column(Float)
    atr: Mapped[float | None] = mapped_column(Float)
    quantity_hint: Mapped[int | None] = mapped_column(Integer)
    product: Mapped[str] = mapped_column(String(8), nullable=False)
    tv_time: Mapped[str | None] = mapped_column(String(32))    # {{timenow}} from TradingView
    bar_time: Mapped[str | None] = mapped_column(String(32))   # {{time}} from TradingView
    interval: Mapped[str | None] = mapped_column(String(8))
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    orders: Mapped[list[Order]] = relationship("Order", back_populates="alert")
    strike_decisions: Mapped[list[StrikeDecision]] = relationship("StrikeDecision", back_populates="alert")
    errors: Mapped[list[AppError]] = relationship("AppError", back_populates="alert")


class Order(Base):
    """Outbound Kite order call — one row per place_order() invocation."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), nullable=False, index=True)
    kite_order_id: Mapped[str | None] = mapped_column(String(32))   # null in DRY_RUN
    variety: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(4), nullable=False)   # BUY / SELL
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    product: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    trigger_price: Mapped[float | None] = mapped_column(Float)
    market_protection: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    fill_price: Mapped[float | None] = mapped_column(Float)
    fill_qty: Mapped[int | None] = mapped_column(Integer)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_response: Mapped[str | None] = mapped_column(Text)
    straddle_id: Mapped[str | None] = mapped_column(String(36))   # links both legs of a straddle

    alert: Mapped[Alert] = relationship("Alert", back_populates="orders")
    position: Mapped[Position | None] = relationship("Position", back_populates="order", uselist=False)
    gtts: Mapped[list[Gtt]] = relationship("Gtt", back_populates="order")


class Gtt(Base):
    """GTT OCO record — placed after entry fills, modified on breakeven/trail."""

    __tablename__ = "gtts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    kite_gtt_id: Mapped[int | None] = mapped_column(Integer)   # null in DRY_RUN
    gtt_type: Mapped[str] = mapped_column(String(8), nullable=False)   # OCO / SINGLE
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    sl_trigger: Mapped[float] = mapped_column(Float, nullable=False)
    target_trigger: Mapped[float] = mapped_column(Float, nullable=False)
    sl_order_price: Mapped[float] = mapped_column(Float, nullable=False)
    target_order_price: Mapped[float] = mapped_column(Float, nullable=False)
    last_price_at_placement: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    modification_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    order: Mapped[Order] = relationship("Order", back_populates="gtts")
    position: Mapped[Position | None] = relationship("Position", back_populates="gtt", uselist=False)


class Position(Base):
    """Open position state — source of truth for breakeven/trail decisions."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, unique=True)
    gtt_id: Mapped[int | None] = mapped_column(ForeignKey("gtts.id"))
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(4), nullable=False)   # CE / PE / FUT / EQ
    # For options: option LTP at fill. For NATURALGAS futures: future price at fill.
    entry_premium: Mapped[float] = mapped_column(Float, nullable=False)
    current_sl: Mapped[float] = mapped_column(Float, nullable=False)   # mirrors active GTT SL leg
    breakeven_moved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trail_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Excursion extremes seen by the trailing manager while registered
    # (MAE/MFE inputs; None when trailing never saw a tick for this position).
    max_favorable_price: Mapped[float | None] = mapped_column(Float)
    max_adverse_price: Mapped[float | None] = mapped_column(Float)

    order: Mapped[Order] = relationship("Order", back_populates="position")
    gtt: Mapped[Gtt | None] = relationship("Gtt", back_populates="position")
    closed_trade: Mapped[ClosedTrade | None] = relationship(
        "ClosedTrade", back_populates="position", uselist=False
    )


class ClosedTrade(Base):
    """Finalized trade — written when SL/target hits or manual exit."""

    __tablename__ = "closed_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), nullable=False, unique=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_premium: Mapped[float] = mapped_column(Float, nullable=False)
    exit_premium: Mapped[float] = mapped_column(Float, nullable=False)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    # SL_HIT / TARGET_HIT / MANUAL_EXIT / SQUAREOFF / KILL_SWITCH
    exit_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    position: Mapped[Position] = relationship("Position", back_populates="closed_trade")


class PnlSnapshot(Base):
    """Intraday P&L sample — written every 5 min by the scheduler during market
    hours to power the /control Today-card sparkline."""

    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    realized: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    open_mtm: Mapped[float | None] = mapped_column(Float)   # null when no Kite session


class KiteSession(Base):
    """Kite access-token lifecycle — kite_session.py writes encrypted ciphertext."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Encrypted by kite_session.py before storage; column holds ciphertext, never plaintext.
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StrikeDecision(Base):
    """Full audit trail for every strike-selection run — all candidates logged."""

    __tablename__ = "strike_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"), nullable=True, index=True)
    underlying: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[str] = mapped_column(String(16), nullable=False)   # ISO date, e.g. "2026-05-01"
    flag: Mapped[str] = mapped_column(String(1), nullable=False)      # "c" or "p"
    target_delta: Mapped[float] = mapped_column(Float, nullable=False)
    # JSON array of all candidate strikes with their delta, IV, LTP, OI, spread.
    candidates_json: Mapped[str] = mapped_column(Text, nullable=False)
    selected_tradingsymbol: Mapped[str | None] = mapped_column(String(64))
    selected_strike: Mapped[float | None] = mapped_column(Float)
    selected_delta: Mapped[float | None] = mapped_column(Float)
    selected_iv: Mapped[float | None] = mapped_column(Float)
    selected_ltp: Mapped[float | None] = mapped_column(Float)
    rejection_reason: Mapped[str | None] = mapped_column(Text)   # set when no valid strike found
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    alert: Mapped[Alert] = relationship("Alert", back_populates="strike_decisions")


class AppError(Base):
    """Caught exceptions — written for every error that reaches the top-level handler."""

    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"))
    error_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    alert: Mapped[Alert | None] = relationship("Alert", back_populates="errors")


class WebAuthnCredential(Base):
    """Single-row table — the admin's registered biometric credential."""

    __tablename__ = "webauthn_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True, index=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebSession(Base):
    """Browser session issued after successful WebAuthn or password login."""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ── Shared session factory (set by main.py lifespan; used by webauthn_routes) ─

_factory: Any = None


def _register_factory(factory: Any) -> None:
    global _factory
    _factory = factory


def get_db_session() -> Any:
    if _factory is None:
        raise RuntimeError("DB session factory not registered")
    with _factory() as sess:
        yield sess


def init_db(database_url: str | None = None) -> Engine:
    """Create SQLAlchemy engine and all tables.

    For SQLite file URLs, creates the parent directory automatically so the
    caller never needs to pre-create data/.
    """
    # Register commodity-agent tables on Base before create_all (lazy import
    # here avoids a circular import at module load).
    import app.commodity_agents.storage  # noqa: F401

    url = database_url or get_settings().DATABASE_URL
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        path = url[len("sqlite:///"):]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    if "sqlite" in url:
        # timeout=30: wait up to 30s for a write lock before raising OperationalError.
        # WAL journal mode lets readers proceed concurrently with the one active writer.
        connect_args = {"check_same_thread": False, "timeout": 30}
        engine = create_engine(url, connect_args=connect_args)
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=30000"))
            conn.commit()
    else:
        engine = create_engine(url)
    Base.metadata.create_all(engine)
    _migrate(engine)
    return engine


def _migrate(engine: Engine) -> None:
    """Apply additive schema changes not handled by create_all (new nullable columns)."""
    migrations = [
        "ALTER TABLE orders ADD COLUMN straddle_id VARCHAR(36)",
        "ALTER TABLE commodity_agent_runs ADD COLUMN analytics_json TEXT",
        "ALTER TABLE commodity_recommendations ADD COLUMN realized_move_pct FLOAT",
        "ALTER TABLE commodity_recommendations ADD COLUMN outcome VARCHAR(16)",
        "ALTER TABLE commodity_recommendations ADD COLUMN suggested_lots INTEGER",
        "ALTER TABLE commodity_trade_journal ADD COLUMN slippage_pct FLOAT",
        "ALTER TABLE commodity_trade_journal ADD COLUMN mae_pct FLOAT",
        "ALTER TABLE commodity_trade_journal ADD COLUMN mfe_pct FLOAT",
        "ALTER TABLE positions ADD COLUMN max_favorable_price FLOAT",
        "ALTER TABLE positions ADD COLUMN max_adverse_price FLOAT",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # column already exists — ignore
