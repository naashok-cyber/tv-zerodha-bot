"""Tests for app/symbol_mapper.py — all offline, no network."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.storage import Base, Instrument
from app.symbol_mapper import (
    NotOrderableError,
    Underlying,
    list_expiries,
    list_strikes,
    refresh_instruments,
    resolve,
    resolve_underlying,
    round_to_tick,
)

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "instruments_sample.csv"
TEST_TODAY = date(2025, 1, 10)
FIXTURE_ROW_COUNT = 20


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def populated_session(session: Session) -> Session:
    csv_text = FIXTURE_CSV.read_text()
    refresh_instruments(session, _download_fn=lambda _url: csv_text)
    return session


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


# ── refresh_instruments ───────────────────────────────────────────────────────

def test_refresh_instruments_loads_rows(populated_session: Session) -> None:
    count = populated_session.query(Instrument).count()
    assert count == FIXTURE_ROW_COUNT


def test_refresh_instruments_is_idempotent(session: Session) -> None:
    csv_text = FIXTURE_CSV.read_text()
    fn = lambda _url: csv_text
    first = refresh_instruments(session, _download_fn=fn)
    second = refresh_instruments(session, _download_fn=fn)
    assert first == second
    assert session.query(Instrument).count() == first


# ── resolve — case a: NSE equity ─────────────────────────────────────────────

def test_resolve_nse_equity(populated_session: Session) -> None:
    inst = resolve("NSE:RELIANCE", "NSE", populated_session)
    assert inst is not None
    assert inst.tradingsymbol == "RELIANCE"
    assert inst.exchange == "NSE"
    assert inst.instrument_type == "EQ"


# ── resolve — case b: index spot raises NotOrderableError ────────────────────

def test_resolve_index_raises_not_orderable(populated_session: Session) -> None:
    with pytest.raises(NotOrderableError):
        resolve("NSE:NIFTY", "NSE", populated_session)


# ── resolve — case c: continuous future (NFO) ────────────────────────────────

def test_resolve_continuous_future_nifty(populated_session: Session) -> None:
    inst = resolve("NIFTY1!", "NFO", populated_session, _today=TEST_TODAY)
    assert inst is not None
    assert inst.name == "NIFTY"
    assert inst.instrument_type == "FUT"
    # nearest unexpired from 2025-01-10 is the Jan expiry (2025-01-30)
    assert inst.expiry == date(2025, 1, 30)


# ── resolve — case d: monthly option ─────────────────────────────────────────

def test_resolve_monthly_option(populated_session: Session) -> None:
    inst = resolve("NIFTY25JAN23500CE", "NFO", populated_session)
    assert inst is not None
    assert inst.tradingsymbol == "NIFTY25JAN23500CE"
    assert inst.instrument_type == "CE"
    assert inst.strike == 23500.0
    assert inst.expiry == date(2025, 1, 30)


# ── resolve — case e: weekly option ──────────────────────────────────────────

def test_resolve_weekly_option(populated_session: Session) -> None:
    inst = resolve("NIFTY2511623500CE", "NFO", populated_session)
    assert inst is not None
    assert inst.tradingsymbol == "NIFTY2511623500CE"
    assert inst.instrument_type == "CE"
    assert inst.expiry == date(2025, 1, 16)


def test_resolve_weekly_option_oct_nov_dec_encoding(populated_session: Session) -> None:
    oct_inst = resolve("NIFTY25O1623500CE", "NFO", populated_session)
    nov_inst = resolve("NIFTY25N1323500CE", "NFO", populated_session)
    dec_inst = resolve("NIFTY25D1123500CE", "NFO", populated_session)
    assert oct_inst is not None and oct_inst.expiry == date(2025, 10, 16)
    assert nov_inst is not None and nov_inst.expiry == date(2025, 11, 13)
    assert dec_inst is not None and dec_inst.expiry == date(2025, 12, 11)


# ── resolve — case f: MCX continuous future ───────────────────────────────────

def test_resolve_mcx_continuous_future(populated_session: Session) -> None:
    inst = resolve("MCX:CRUDEOIL1!", "MCX", populated_session, _today=TEST_TODAY)
    assert inst is not None
    assert inst.name == "CRUDEOIL"
    assert inst.exchange == "MCX"
    assert inst.instrument_type == "FUT"
    # nearest unexpired from 2025-01-10 is Jan expiry (2025-01-17)
    assert inst.expiry == date(2025, 1, 17)


# ── resolve — missing symbol returns None ─────────────────────────────────────

def test_resolve_missing_symbol_returns_none(populated_session: Session) -> None:
    result = resolve("NSE:XYZNOTEXIST", "NSE", populated_session)
    assert result is None


# ── resolve_underlying ────────────────────────────────────────────────────────

def test_resolve_underlying_nifty_spot_source() -> None:
    u = resolve_underlying("NSE:NIFTY", settings=_settings())
    assert u.name == "NIFTY"
    assert u.segment == "NFO"
    assert u.is_natural_gas is False
    assert u.spot_source == "NSE:NIFTY 50"


def test_resolve_underlying_naturalgas_flagged() -> None:
    u = resolve_underlying("MCX:NATURALGAS", settings=_settings())
    assert u.is_natural_gas is True
    assert u.segment == "MCX"


def test_resolve_underlying_natgasmini_flagged() -> None:
    u = resolve_underlying("MCX:NATGASMINI", settings=_settings())
    assert u.is_natural_gas is True


# ── round_to_tick ─────────────────────────────────────────────────────────────

def test_round_to_tick_below_half_rounds_down() -> None:
    result = round_to_tick(Decimal("123.07"), Decimal("0.05"))
    assert result == Decimal("123.05")
    assert isinstance(result, Decimal)


def test_round_to_tick_at_half_rounds_up() -> None:
    result = round_to_tick(Decimal("123.075"), Decimal("0.05"))
    assert result == Decimal("123.10")
    assert isinstance(result, Decimal)


# ── list_strikes ──────────────────────────────────────────────────────────────

def test_list_strikes_returns_sorted_strikes(populated_session: Session) -> None:
    strikes = list_strikes("NIFTY", date(2025, 1, 16), "CE", populated_session)
    assert strikes == sorted(strikes)
    assert 23000.0 in strikes
    assert 23500.0 in strikes
    assert 24000.0 in strikes


# ── list_expiries ─────────────────────────────────────────────────────────────

def test_list_expiries_returns_sorted_expiries(populated_session: Session) -> None:
    expiries = list_expiries("NIFTY", "CE", populated_session)
    assert expiries == sorted(expiries)
    assert date(2025, 1, 16) in expiries
    assert date(2025, 1, 23) in expiries
    assert date(2025, 1, 30) in expiries
