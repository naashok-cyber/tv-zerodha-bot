"""Shared pytest fixtures — apply to the entire test suite."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import app.state as state


@pytest.fixture(autouse=True)
def reset_session_invalid():
    """Guarantee SESSION_INVALID is False before and after every test."""
    state.set_session_invalid(False)
    yield
    state.set_session_invalid(False)


@pytest.fixture(autouse=True)
def mock_lifespan_scheduler(monkeypatch):
    """Replace make_scheduler and daily_session_check in main.py lifespan.

    Prevents a real APScheduler from starting (and its background threads
    from calling kite.profile()) during TestClient-based tests.  Scheduler
    unit tests patch at the app.scheduler level instead, so this fixture
    does not interfere with them.
    """
    mock_sched = MagicMock()
    monkeypatch.setattr("app.main.make_scheduler", lambda *a, **kw: mock_sched)
    monkeypatch.setattr("app.main.daily_session_check", lambda *a, **kw: None)
