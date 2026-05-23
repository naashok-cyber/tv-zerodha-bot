"""Shared pytest fixtures — apply to the entire test suite."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import app.state as state


@pytest.fixture(autouse=True)
def reset_app_state():
    """Reset all app.state overrides before and after every test."""
    def _clear():
        state.set_session_invalid(False)
        state.set_paper_mode(None)
        state.set_emergency_stop(False)
        state.set_entry_window_start(None)
        state.set_entry_window_end(None)
        state.set_no_entry_on_expiry_day(None)
        state.set_trailing_enabled(True)
        state.set_max_lots(None)
        state.set_max_daily_loss(None)
        state.set_sl_pct(None)
        state.set_rr_ratio(None)
        state.set_daily_profit_target(None)
        state.set_sell_options_profit_pct(None)
        state.set_max_trades_per_day(None)
        state.set_max_open_positions(None)
        state.set_capital_per_trade(None)
        state.set_consecutive_losses_limit(None)

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def mock_lifespan_scheduler(monkeypatch):
    """Replace scheduler + get_session_manager in main.py lifespan.

    Prevents a real APScheduler from starting and KiteSessionManager from
    requiring SECRET_KEY during TestClient-based tests.  Tests that need a
    specific session-manager mock set their own via monkeypatch or patch().
    """
    mock_sched = MagicMock()
    mock_session_mgr = MagicMock()
    monkeypatch.setattr("app.main.make_scheduler", lambda *a, **kw: mock_sched)
    monkeypatch.setattr("app.main.daily_session_check", lambda *a, **kw: None)
    monkeypatch.setattr("app.main.get_session_manager", lambda: mock_session_mgr)
