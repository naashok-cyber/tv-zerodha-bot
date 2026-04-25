from __future__ import annotations

import os
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
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
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), nullable=False, index=True)
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


def init_db(database_url: str | None = None) -> Engine:
    """Create SQLAlchemy engine and all tables.

    For SQLite file URLs, creates the parent directory automatically so the
    caller never needs to pre-create data/.
    """
    url = database_url or get_settings().DATABASE_URL
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        path = url[len("sqlite:///"):]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    connect_args = {"check_same_thread": False} if "sqlite" in url else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return engine
