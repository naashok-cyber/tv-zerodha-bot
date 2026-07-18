"""Tests for app/straddle_defense.py — Phase 1 monitor + alert logic."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.straddle_defense as sd
from app.config import IST, Settings
from app.storage import Base, IvSnapshot

NOW = datetime(2026, 7, 17, 18, 5, tzinfo=IST)


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        KITE_API_KEY="fake",
        SECRET_KEY="",
        DASHBOARD_PASSWORD="",
        PBKDF2_ITERATIONS=1,
        STRADDLE_DEFENSE_ENABLED=True,
        STRADDLE_DEFENSE_DRAWDOWN_TRIGGER=5000.0,
        STRADDLE_DEFENSE_IV_SAMPLES=3,
        STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY=2,
        STRADDLE_DEFENSE_REARM_MINUTES=20,
        STRADDLE_DEFENSE_PREHEDGE_TIME="17:45",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "_STATE_PATH", str(tmp_path / "sd_state.json"))
    sd.reset_state_for_tests()
    yield
    sd.reset_state_for_tests()


def _factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ── iv_rising ─────────────────────────────────────────────────────────────────

def test_iv_rising_true_on_strict_rise():
    assert sd.iv_rising([30.0, 30.2, 30.5, 30.9], 3) is True


def test_iv_rising_false_when_one_step_flat_or_down():
    assert sd.iv_rising([30.0, 30.5, 30.5, 30.9], 3) is False
    assert sd.iv_rising([30.0, 30.5, 30.3, 30.9], 3) is False


def test_iv_rising_false_with_insufficient_samples():
    assert sd.iv_rising([30.0, 30.5, 30.9], 3) is False
    assert sd.iv_rising([], 3) is False


# ── evaluate_straddle: trigger + hysteresis ───────────────────────────────────

def _fresh_state():
    return {"date": NOW.date().isoformat(), "prehedge_sent": False, "straddles": {}}


def test_no_alert_below_drawdown_trigger():
    st = _fresh_state()
    s = _settings()
    rising = [30.0, 30.4, 30.9, 31.5]
    assert sd.evaluate_straddle("k1", 1000.0, rising, s, st, NOW) is None   # peak=1000
    assert sd.evaluate_straddle("k1", -3000.0, rising, s, st, NOW) is None  # dd=4000 < 5000


def test_alert_fires_on_drawdown_and_rising_iv():
    st = _fresh_state()
    s = _settings()
    rising = [30.0, 30.4, 30.9, 31.5]
    sd.evaluate_straddle("k1", 2000.0, rising, s, st, NOW)          # sets peak
    dd = sd.evaluate_straddle("k1", -3500.0, rising, s, st, NOW)    # dd=5500
    assert dd == 5500.0
    assert st["straddles"]["k1"]["alerts"] == 1


def test_no_alert_when_iv_not_rising():
    st = _fresh_state()
    s = _settings()
    flat = [31.5, 31.5, 31.5, 31.5]
    sd.evaluate_straddle("k1", 2000.0, flat, s, st, NOW)
    assert sd.evaluate_straddle("k1", -3500.0, flat, s, st, NOW) is None


def test_rearm_gap_suppresses_second_alert():
    st = _fresh_state()
    s = _settings()
    rising = [30.0, 30.4, 30.9, 31.5]
    sd.evaluate_straddle("k1", 2000.0, rising, s, st, NOW)
    assert sd.evaluate_straddle("k1", -3500.0, rising, s, st, NOW) is not None
    soon = NOW + timedelta(minutes=5)
    assert sd.evaluate_straddle("k1", -4000.0, rising, s, st, soon) is None
    later = NOW + timedelta(minutes=25)
    assert sd.evaluate_straddle("k1", -4000.0, rising, s, st, later) is not None


def test_max_alerts_per_day_cap():
    st = _fresh_state()
    s = _settings()
    rising = [30.0, 30.4, 30.9, 31.5]
    sd.evaluate_straddle("k1", 2000.0, rising, s, st, NOW)
    assert sd.evaluate_straddle("k1", -3500.0, rising, s, st, NOW) is not None
    t2 = NOW + timedelta(minutes=30)
    assert sd.evaluate_straddle("k1", -5000.0, rising, s, st, t2) is not None
    t3 = NOW + timedelta(minutes=60)
    assert sd.evaluate_straddle("k1", -9000.0, rising, s, st, t3) is None  # cap = 2


def test_peak_ratchets_up_only():
    st = _fresh_state()
    s = _settings()
    flat = [31.0] * 4
    sd.evaluate_straddle("k1", 1000.0, flat, s, st, NOW)
    sd.evaluate_straddle("k1", 4200.0, flat, s, st, NOW)
    sd.evaluate_straddle("k1", 3000.0, flat, s, st, NOW)
    assert st["straddles"]["k1"]["peak"] == 4200.0


# ── the job ───────────────────────────────────────────────────────────────────

def _mock_greeks(mtm: float, iv_pct: float | None, short: bool = True) -> dict:
    return {
        "positions": [],
        "straddles": {"sid-1": {
            "underlying": "CRUDEOILM", "legs": ["CE", "PE"],
            "net_delta_per_lot": 0.02, "mtm": mtm,
            "iv_mean_pct": iv_pct, "short": short,
        }},
        "totals": {}, "margins": None,
    }


def test_job_noop_when_disabled(monkeypatch):
    import app.state as state
    monkeypatch.setattr(state, "is_straddle_defense_enabled", lambda d: False)
    kite_mgr = MagicMock()
    with patch("app.kite_session.get_session_manager", kite_mgr):
        sd.straddle_defense_job(_settings(), _factory())
    kite_mgr.assert_not_called()


def test_job_writes_snapshot_and_alerts(monkeypatch):
    import app.state as state
    monkeypatch.setattr(state, "is_straddle_defense_enabled", lambda d: True)
    factory = _factory()
    sent: list[str] = []

    # seed a rising IV series + a high peak in state (relative to real now —
    # the job stamps datetime.now(IST) internally)
    now_real = datetime.now(IST)
    with factory() as s:
        for i, iv in enumerate([30.0, 30.4, 30.9]):
            s.add(IvSnapshot(at=now_real - timedelta(minutes=3 - i),
                             underlying="CRUDEOILM", straddle_key="sid-1",
                             mtm=2000.0, iv_pct=iv))
        s.commit()
    st = sd._today_state(now_real)
    st["straddles"]["sid-1"] = {"peak": 2000.0, "alerts": 0, "last_alert": None}

    with (
        patch("app.kite_session.get_session_manager", return_value=MagicMock()),
        patch("app.commodity_agents.portfolio.compute_portfolio_greeks",
              return_value=_mock_greeks(mtm=-3500.0, iv_pct=31.5)),
        patch("app.commodity_agents.notify.send_telegram",
              side_effect=lambda settings, text: sent.append(text) or True),
    ):
        sd.straddle_defense_job(_settings(), factory)

    with factory() as s:
        n = s.query(IvSnapshot).count()
    assert n == 4                     # 3 seeded + 1 new
    assert len(sent) == 1
    assert "CRUDEOILM" in sent[0]
    assert "5,500" in sent[0]


def test_job_no_alert_for_long_straddles(monkeypatch):
    import app.state as state
    monkeypatch.setattr(state, "is_straddle_defense_enabled", lambda d: True)
    factory = _factory()
    sent: list[str] = []
    with (
        patch("app.kite_session.get_session_manager", return_value=MagicMock()),
        patch("app.commodity_agents.portfolio.compute_portfolio_greeks",
              return_value=_mock_greeks(mtm=-9000.0, iv_pct=35.0, short=False)),
        patch("app.commodity_agents.notify.send_telegram",
              side_effect=lambda settings, text: sent.append(text) or True),
    ):
        sd.straddle_defense_job(_settings(), factory)
    with factory() as s:
        assert s.query(IvSnapshot).count() == 0   # long straddles not tracked
    assert sent == []
