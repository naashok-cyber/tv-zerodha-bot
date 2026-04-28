from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import requests
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.storage import Instrument

_KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments"

# Single-character month encoding used in Kite weekly option tradingsymbols.
_MONTH_CHAR: dict[str, int] = {
    **{str(i): i for i in range(1, 10)},
    "O": 10,
    "N": 11,
    "D": 12,
}

_MONTH_ABBR: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_INDEX_NAMES = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"})

# MCX commodities that should route to MCX segment even without an "MCX:" exchange prefix.
# NATURALGAS/NATGASMINI are kept separate (routed to near-month futures via is_natural_gas).
# Add any new MCX commodity here to avoid it falling through to the NSE equity path.
_MCX_COMMODITY_NAMES = frozenset({
    "CRUDEOIL", "CRUDEOILM",
    "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL",
    "SILVER", "SILVERM", "SILVERMIC",
    "COPPER", "LEAD", "LEADMINI", "ZINC", "ZINCMINI",
    "ALUMINIUM", "NICKEL",
    "MENTHAOIL", "COTTON", "CASTORSEED",
})

_INDEX_SPOT: dict[str, str] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX": "BSE:SENSEX",
}

_INDEX_SEGMENT: dict[str, str] = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX": "BFO",
}

# Patterns for classifying option/future tradingsymbols.
_RE_MONTHLY_OPT = re.compile(
    r"^([A-Z]+)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+)(CE|PE)$"
)
_RE_WEEKLY_OPT = re.compile(r"^([A-Z]+)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$")
_RE_MONTHLY_FUT = re.compile(
    r"^([A-Z]+)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)FUT$"
)


class NotOrderableError(Exception):
    """tv_ticker resolves to a spot index; the caller must route to options/futures."""


@dataclass(frozen=True)
class Underlying:
    name: str
    segment: str
    is_natural_gas: bool
    spot_source: str


# ── Download & parse ──────────────────────────────────────────────────────────

def _download_csv(url: str) -> str:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def _parse_instruments(csv_text: str) -> list[dict]:
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        expiry_str = row.get("expiry", "").strip()
        strike_str = row.get("strike", "").strip()
        rows.append({
            "instrument_token": int(row["instrument_token"]),
            "exchange_token": int(row["exchange_token"]),
            "tradingsymbol": row["tradingsymbol"].strip(),
            "name": row["name"].strip(),
            "expiry": date.fromisoformat(expiry_str) if expiry_str else None,
            "strike": float(strike_str) if strike_str else None,
            "tick_size": float(row["tick_size"]),
            "lot_size": int(row["lot_size"]),
            "instrument_type": row["instrument_type"].strip(),
            "segment": row["segment"].strip(),
            "exchange": row["exchange"].strip(),
        })
    return rows


def refresh_instruments(
    session: Session,
    settings: Settings | None = None,
    _download_fn: Callable[[str], str] | None = None,
) -> int:
    """Download the Kite instruments CSV, truncate and reload the instruments table.

    Returns the number of rows loaded. Safe to call repeatedly (idempotent).
    Pass _download_fn in tests to avoid network I/O.
    """
    fn = _download_fn or _download_csv
    csv_text = fn(_KITE_INSTRUMENTS_URL)
    rows = _parse_instruments(csv_text)
    session.query(Instrument).delete(synchronize_session=False)
    for row in rows:
        session.add(Instrument(**row))
    session.commit()
    return len(rows)


# ── Resolution helpers ────────────────────────────────────────────────────────

def _resolve_continuous(
    name: str,
    exchange_hint: str,
    session: Session,
    _today: date | None,
) -> Instrument | None:
    today = _today or date.today()
    exchange = exchange_hint if exchange_hint in ("MCX", "NFO") else "NFO"
    return (
        session.query(Instrument)
        .filter(
            Instrument.name == name,
            Instrument.instrument_type == "FUT",
            Instrument.exchange == exchange,
            Instrument.expiry >= today,
        )
        .order_by(Instrument.expiry)
        .first()
    )


# ── Public API ────────────────────────────────────────────────────────────────

def resolve(
    tv_ticker: str,
    tv_exchange: str,
    session: Session,
    hint: str | None = None,
    _today: date | None = None,
) -> Instrument | None:
    """Map a TradingView ticker to a Kite Instrument row.

    Returns None when the symbol cannot be resolved (caller maps to HTTP 422).
    Raises NotOrderableError when tv_ticker is a spot index symbol.
    """
    if ":" in tv_ticker:
        exchange_prefix, symbol = tv_ticker.split(":", 1)
        exchange_prefix = exchange_prefix.upper()
    else:
        exchange_prefix = tv_exchange.upper() if tv_exchange else ""
        symbol = tv_ticker

    symbol = symbol.upper()

    # Continuous future — NIFTY1!, CRUDEOIL1!, etc.
    if symbol.endswith("1!"):
        return _resolve_continuous(symbol[:-2], exchange_prefix, session, _today)

    # Spot index — not directly orderable
    if symbol in _INDEX_NAMES:
        raise NotOrderableError(
            f"{symbol!r} is a spot index; route to options or futures instead."
        )

    # Named option or future — tradingsymbol IS the Kite key; direct lookup.
    if (
        _RE_MONTHLY_OPT.match(symbol)
        or _RE_WEEKLY_OPT.match(symbol)
        or _RE_MONTHLY_FUT.match(symbol)
    ):
        return (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == symbol)
            .first()
        )

    # Equity
    return (
        session.query(Instrument)
        .filter(
            Instrument.tradingsymbol == symbol,
            Instrument.exchange == exchange_prefix,
            Instrument.instrument_type == "EQ",
        )
        .first()
    )


def resolve_underlying(
    tv_ticker: str,
    settings: Settings | None = None,
) -> Underlying:
    """Return structured metadata about the underlying asset for a TV ticker."""
    s = settings or get_settings()

    if ":" in tv_ticker:
        exchange_prefix, name = tv_ticker.split(":", 1)
        exchange_prefix = exchange_prefix.upper()
    else:
        exchange_prefix = ""
        name = tv_ticker

    name = name.upper()
    if name.endswith("1!"):
        name = name[:-2]

    is_natural_gas = name in s.NATURAL_GAS_NAMES

    if name in _INDEX_SPOT:
        return Underlying(
            name=name,
            segment=_INDEX_SEGMENT[name],
            is_natural_gas=False,
            spot_source=_INDEX_SPOT[name],
        )

    if exchange_prefix == "MCX" or is_natural_gas or name in _MCX_COMMODITY_NAMES:
        return Underlying(
            name=name,
            segment="MCX",
            is_natural_gas=is_natural_gas,
            spot_source=f"MCX:{name}",
        )

    return Underlying(
        name=name,
        segment="NSE",
        is_natural_gas=False,
        spot_source=f"NSE:{name}",
    )


def round_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round price to the nearest tick_size multiple using Decimal arithmetic."""
    return (price / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def list_strikes(
    name: str,
    expiry: date,
    instrument_type: str,
    session: Session,
) -> list[float]:
    """Return sorted list of distinct strikes for a given name/expiry/type."""
    rows = (
        session.query(Instrument.strike)
        .filter(
            Instrument.name == name,
            Instrument.expiry == expiry,
            Instrument.instrument_type == instrument_type,
            Instrument.strike.isnot(None),
        )
        .distinct()
        .order_by(Instrument.strike)
        .all()
    )
    return [r[0] for r in rows]


def list_expiries(
    name: str,
    instrument_type: str,
    session: Session,
) -> list[date]:
    """Return sorted list of distinct expiry dates for a given name/type."""
    rows = (
        session.query(Instrument.expiry)
        .filter(
            Instrument.name == name,
            Instrument.instrument_type == instrument_type,
            Instrument.expiry.isnot(None),
        )
        .distinct()
        .order_by(Instrument.expiry)
        .all()
    )
    return [r[0] for r in rows]
