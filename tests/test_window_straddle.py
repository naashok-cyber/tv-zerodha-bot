"""Tests for app/window_straddle.py — spread guard and skip logic."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.state as state
from app.config import IST, Settings
from app.storage import Base
from app.voice.straddle import StraddleLeg, StraddleValidation
from app.window_straddle import run_window_straddle_entry


# ── Helpers ────────────────────────────────────────────────────────────────────

def _settings(**kwargs) -> Settings:
    defaults = dict(
        _env_file=None,
        DRY_RUN=True,
        KITE_API_KEY="testkey",
        DATABASE_URL="sqlite:///:memory:",
        DASHBOARD_PASSWORD="",
        SECRET_KEY="",
        PBKDF2_ITERATIONS=1,
        STRADDLE_MAX_SPREAD_PCT=1.0,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _make_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _leg(valid: bool, spread_pct: float) -> StraddleLeg:
    return StraddleLeg(
        tradingsymbol="NATURALGAS26JUL300CE",
        instrument_token=123456,
        strike=300.0,
        ltp=10.0,
        bid=9.9 if valid else 0.1,
        ask=10.1 if valid else 14.7,
        spread_abs=0.2 if valid else 14.6,
        spread_pct=spread_pct,
        threshold_used=0.1,
        valid=valid,
    )


def _validation(spread_ok: bool, margin_ok: bool = True) -> StraddleValidation:
    ce_pct = 0.5 if spread_ok else 145.93
    pe_pct = 0.5 if spread_ok else 145.93
    all_ok = spread_ok and margin_ok
    block = None if all_ok else (
        "spread too wide" if not spread_ok else "insufficient margin"
    )
    return StraddleValidation(
        underlying="NATURALGAS",
        atm_strike=300.0,
        expiry=date(2026, 7, 17),
        quantity=2,
        lot_size=1,
        lot_units=1250,
        futures_ltp=300.0,
        ce=_leg(spread_ok, ce_pct),
        pe=_leg(spread_ok, pe_pct),
        net_credit_per_lot=20.0,
        estimated_sl_per_lot=30.0,
        sl_multiplier=1.5,
        net_credit_total=50000.0,
        margin_required=100000.0,
        margin_available=200000.0 if margin_ok else 50000.0,
        margin_ok=margin_ok,
        spread_ok=spread_ok,
        all_ok=all_ok,
        block_reason=block,
    )


def _run(validation: StraddleValidation, process_straddle_mock: MagicMock) -> None:
    """Call run_window_straddle_entry with all external deps mocked.

    window_straddle.py uses late imports inside the function body, so we
    patch at the source module where each name is defined.
    """
    mock_kite = MagicMock()
    mock_sm = MagicMock()
    mock_sm.get_kite.return_value = mock_kite

    with (
        patch("app.kite_session.get_session_manager", return_value=mock_sm),
        patch("app.voice.straddle.validate_straddle", return_value=validation),
        patch("app.main._process_straddle", process_straddle_mock),
    ):
        run_window_straddle_entry(
            underlying="NATURALGAS",
            exchange="MCX",
            qty=2,
            monthly_only=False,
            entry_hhmm="22:10",
            settings=_settings(),
            session_factory=_make_factory(),
        )


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestSpreadGuard:
    def setup_method(self):
        state.set_window_straddle_enabled(True)
        state.set_session_invalid(False)
        state.set_emergency_stop(False)

    def teardown_method(self):
        state.set_window_straddle_enabled(False)

    def test_wide_spread_skips_trade(self):
        """PE spread 145.93% must not result in a trade — this was today's ₹15k bug."""
        mock_process = MagicMock()
        _run(_validation(spread_ok=False), mock_process)
        mock_process.assert_not_called()

    def test_ok_spread_places_trade(self):
        """When spread is within 1%, the straddle should proceed."""
        mock_process = MagicMock()
        _run(_validation(spread_ok=True), mock_process)
        mock_process.assert_called_once()

    def test_insufficient_margin_skips_trade(self):
        """Margin check must also gate the trade independently of spread."""
        mock_process = MagicMock()
        _run(_validation(spread_ok=True, margin_ok=False), mock_process)
        mock_process.assert_not_called()

    def test_both_bad_skips_trade(self):
        """Wide spread + bad margin → still skips (margin check fires first)."""
        mock_process = MagicMock()
        _run(_validation(spread_ok=False, margin_ok=False), mock_process)
        mock_process.assert_not_called()


class TestStrategyGates:
    def setup_method(self):
        state.set_session_invalid(False)
        state.set_emergency_stop(False)

    def teardown_method(self):
        state.set_window_straddle_enabled(False)

    def test_strategy_disabled_skips(self):
        """If window straddle is toggled off, no validate_straddle call at all."""
        state.set_window_straddle_enabled(False)
        mock_process = MagicMock()

        with (
            patch("app.voice.straddle.validate_straddle") as mock_validate,
            patch("app.main._process_straddle", mock_process),
        ):
            mock_validate.return_value = _validation(spread_ok=True)
            run_window_straddle_entry(
                underlying="NATURALGAS", exchange="MCX", qty=2,
                monthly_only=False, entry_hhmm="22:10",
                settings=_settings(), session_factory=_make_factory(),
            )
            mock_validate.assert_not_called()
            mock_process.assert_not_called()

    def test_session_invalid_skips(self):
        """SESSION_INVALID gate prevents entry even with perfect spread."""
        state.set_window_straddle_enabled(True)
        state.set_session_invalid(True)
        mock_process = MagicMock()

        with (
            patch("app.voice.straddle.validate_straddle") as mock_validate,
            patch("app.main._process_straddle", mock_process),
        ):
            run_window_straddle_entry(
                underlying="NATURALGAS", exchange="MCX", qty=2,
                monthly_only=False, entry_hhmm="22:10",
                settings=_settings(), session_factory=_make_factory(),
            )
            mock_validate.assert_not_called()
            mock_process.assert_not_called()

    def test_emergency_stop_skips(self):
        """Emergency stop prevents entry."""
        state.set_window_straddle_enabled(True)
        state.set_emergency_stop(True)
        mock_process = MagicMock()

        with (
            patch("app.voice.straddle.validate_straddle") as mock_validate,
            patch("app.main._process_straddle", mock_process),
        ):
            run_window_straddle_entry(
                underlying="NATURALGAS", exchange="MCX", qty=2,
                monthly_only=False, entry_hhmm="22:10",
                settings=_settings(), session_factory=_make_factory(),
            )
            mock_validate.assert_not_called()
            mock_process.assert_not_called()
