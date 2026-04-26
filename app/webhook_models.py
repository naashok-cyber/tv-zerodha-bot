from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"
    TRAIL = "TRAIL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class TradingViewAlert(BaseModel):
    """Strict Pydantic model for TradingView webhook JSON payloads.

    Uses extra="forbid" to reject unknown fields immediately.
    The "time" alias maps TradingView's {{timenow}} field to tv_time.
    """

    model_config = ConfigDict(extra="forbid")

    secret: str
    strategy_id: str
    tv_ticker: str
    tv_exchange: str
    action: Action
    order_type: OrderType | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    sl_percent: float | None = None
    atr: float | None = None
    quantity_hint: int | None = None
    product: str = "NRML"
    tv_time: str | None = Field(None, alias="time")  # {{timenow}} in TradingView
    bar_time: str | None = None                       # {{time}} in TradingView
    interval: str | None = None

    def idempotency_key(self) -> str:
        """SHA-256 of (strategy_id, tv_ticker, bar_time, action) — dedup handle."""
        raw = f"{self.strategy_id}:{self.tv_ticker}:{self.bar_time or ''}:{self.action.value}"
        return hashlib.sha256(raw.encode()).hexdigest()
