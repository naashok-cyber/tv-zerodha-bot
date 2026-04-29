from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.config import IST, UTC


class AlertPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str                                    # e.g. "NIFTY", "NATURALGAS", "RELIANCE"
    action: Literal["BUY", "SELL", "EXIT", "TRAIL"]
    instrument_type: Literal["OPTIONS", "EQUITY"] = "OPTIONS"  # defaults to OPTIONS so existing alerts keep working
    price: Decimal                                 # current price from TradingView
    premium: Decimal | None = None                 # required for TRAIL action only
    timeframe: str                                 # e.g. "5", "15", "60"
    alert_id: str                                  # unique ID from Pine Script
    timestamp: datetime                            # ISO8601, TradingView sends UTC

    @field_validator("symbol")
    @classmethod
    def symbol_valid(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be empty")
        if len(v) > 20:
            raise ValueError("symbol must be at most 20 characters")
        return v

    @field_validator("price")
    @classmethod
    def price_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("price must be > 0")
        return v

    @field_validator("alert_id")
    @classmethod
    def alert_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("alert_id must not be empty")
        return v

    @field_validator("timestamp")
    @classmethod
    def convert_to_ist(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            # TradingView {{time}} sends UTC without a timezone suffix; treat as UTC.
            v = v.replace(tzinfo=UTC)
        return v.astimezone(IST)

    @model_validator(mode="after")
    def premium_required_for_trail(self) -> "AlertPayload":
        if self.action == "TRAIL" and self.premium is None:
            raise ValueError("premium is required when action is TRAIL")
        return self
