"""Shared dataclasses passed between pipeline stages.

Everything here is plain data — no I/O, no Kite, no LLM — so every stage can be
unit-tested by constructing these directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

COMMODITIES = ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER", "NIFTY", "BANKNIFTY")

# NSE index underlyings trade NFO options priced off index spot (BSM);
# everything else here is MCX options priced off the future (Black-76).
INDEX_UNDERLYINGS = ("NIFTY", "BANKNIFTY")


def exchange_for(underlying: str) -> str:
    return "NFO" if underlying in INDEX_UNDERLYINGS else "MCX"


# One macro shock hits a whole group at once — a correlated short-vega book is
# one bad night away from simultaneous max losses. The Risk Guard allows at
# most one open short-premium underlying per group. Mini contracts count with
# their parent.
CORRELATION_GROUPS = {
    "energy": ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"),
    "metals": ("GOLD", "GOLDM", "SILVER", "SILVERM"),
    "indices": ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"),
}


def group_for(underlying: str) -> str | None:
    for name, members in CORRELATION_GROUPS.items():
        if underlying in members:
            return name
    return None

# Regime labels (deterministic classifier output)
TRENDING = "trending"
RANGE_BOUND = "range-bound"
HIGH_VOL_EXPANSION = "high-vol-expansion"
PRE_EVENT_COMPRESSION = "pre-event-compression"


@dataclass(frozen=True)
class EventWindow:
    """A scheduled high-impact event with its blackout window (IST datetimes)."""

    name: str
    commodity: str
    event_time: datetime
    blackout_start: datetime
    blackout_end: datetime
    impact: str  # "high" | "medium"


@dataclass(frozen=True)
class RegimeSnapshot:
    label: str
    adx: float | None
    bb_width_pct: float | None        # (upper-lower)/mid on last 20 closes, in %
    realized_vol: float | None        # annualized, from candle closes
    atm_iv: float | None              # ATM option IV (Black-76), if resolvable
    iv_rv_ratio: float | None         # atm_iv / realized_vol
    iv_percentile: float | None       # rank of atm_iv vs stored history, 0-100
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "adx": self.adx,
            "bb_width_pct": self.bb_width_pct,
            "realized_vol": self.realized_vol,
            "atm_iv": self.atm_iv,
            "iv_rv_ratio": self.iv_rv_ratio,
            "iv_percentile": self.iv_percentile,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class StrikeCandidate:
    tradingsymbol: str
    instrument_type: str   # CE | PE
    strike: float
    expiry: str            # ISO date
    ltp: float
    iv: float | None
    delta: float | None
    target_delta: float    # which delta bucket this candidate was picked for
    lot_units: int

    def to_dict(self) -> dict:
        return {
            "tradingsymbol": self.tradingsymbol,
            "instrument_type": self.instrument_type,
            "strike": self.strike,
            "expiry": self.expiry,
            "ltp": self.ltp,
            "iv": self.iv,
            "delta": self.delta,
            "target_delta": self.target_delta,
            "lot_units": self.lot_units,
        }


@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    reasons: list[str]     # empty when approved; populated with veto reasons

    def to_dict(self) -> dict:
        return {"approved": self.approved, "reasons": self.reasons}
