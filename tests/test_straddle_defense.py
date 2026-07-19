"""Tests for app/straddle_defense.py — monitor/alert logic + wing-hedge execution."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.straddle_defense as sd
from app.config import IST, Settings
from app.storage import (
    Alert,
    Base,
    ClosedTrade,
    Gtt,
    HedgeAction,
    Instrument,
    IvSnapshot,
    Order,
    Position,
)

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


def _job_settings(**overrides) -> Settings:
    """Settings for job-level tests: the time-of-day windows (pre-hedge,
    scheduled/forced unwind) are disabled so results don't depend on the
    wall-clock IST time the suite happens to run at."""
    defaults = dict(
        STRADDLE_DEFENSE_PREHEDGE_TIME="",
        STRADDLE_DEFENSE_UNWIND_TIME="",
        STRADDLE_DEFENSE_FORCE_UNWIND_TIME="",
    )
    defaults.update(overrides)
    return _settings(**defaults)


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
        sd.straddle_defense_job(_job_settings(), _factory())
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
        sd.straddle_defense_job(_job_settings(), factory)

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
        sd.straddle_defense_job(_job_settings(), factory)
    with factory() as s:
        assert s.query(IvSnapshot).count() == 0   # long straddles not tracked
    assert sent == []


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — wing hedge execution
# ══════════════════════════════════════════════════════════════════════════════

_EXPIRY = date(2026, 7, 28)
_UNITS = 10          # MCX_LOT_UNITS["CRUDEOILM"]


def _seed_straddle(session, key="sid-1", underlying="CRUDEOILM", strike=5300.0,
                   interval=50.0, qty=5, with_gtts=True,
                   drop_ce_wing_at_steps: int | None = None) -> None:
    """Short CE+PE at `strike` plus a chain of strikes ±3 intervals so wing
    selection has something to pick from. Optionally omits the CE wing at a
    given step count to exercise the fallback search."""
    tok = [1000]

    def _inst(flag: str, k: float) -> Instrument:
        tok[0] += 1
        return Instrument(
            instrument_token=tok[0], exchange_token=tok[0],
            tradingsymbol=f"{underlying}26JUL{int(k)}{flag}", name=underlying,
            expiry=_EXPIRY, strike=k, tick_size=0.1, lot_size=1,
            instrument_type=flag, segment="MCX-OPT", exchange="MCX",
        )

    for i in range(-3, 4):
        k = strike + i * interval
        if drop_ce_wing_at_steps is None or i != drop_ce_wing_at_steps:
            session.add(_inst("CE", k))
        session.add(_inst("PE", k))

    alert = Alert(received_at=NOW, strategy_id="seed", tv_ticker=underlying,
                  tv_exchange="MCX", action="STRADDLE_SHORT", order_type="MARKET",
                  product="NRML", idempotency_key=f"seed-{key}", raw_payload="{}")
    session.add(alert)
    session.flush()
    for flag in ("CE", "PE"):
        o = Order(alert_id=alert.id, kite_order_id=f"K{flag}{key}", variety="regular",
                  exchange="MCX", tradingsymbol=f"{underlying}26JUL{int(strike)}{flag}",
                  transaction_type="SELL", order_type="MARKET", product="NRML",
                  quantity=qty, status="COMPLETE", fill_price=100.0,
                  placed_at=NOW, updated_at=NOW, dry_run=False, straddle_id=key)
        session.add(o)
        session.flush()
        session.add(Position(
            order_id=o.id, exchange="MCX",
            tradingsymbol=o.tradingsymbol, underlying=underlying,
            instrument_type=flag, entry_premium=100.0, current_sl=150.0,
            quantity=qty, lot_size=1, opened_at=NOW, last_updated_at=NOW,
        ))
        if with_gtts:
            session.add(Gtt(
                order_id=o.id, kite_gtt_id=9000 + o.id, gtt_type="OCO",
                exchange="MCX", tradingsymbol=o.tradingsymbol,
                sl_trigger=150.0, target_trigger=50.0, sl_order_price=151.0,
                target_order_price=49.5, last_price_at_placement=100.0,
                status="ACTIVE", placed_at=NOW, updated_at=NOW, dry_run=False,
            ))
    session.commit()


def _quote_mock(px: float = 40.0):
    """kite.quote stub: every key gets ask=px via depth."""
    def _quote(keys):
        return {k: {"last_price": px - 1,
                    "depth": {"sell": [{"price": px}], "buy": [{"price": px - 2}]}}
                for k in keys}
    return _quote


# ── wing selection ────────────────────────────────────────────────────────────

def test_select_wings_picks_otm_strikes():
    factory = _factory()
    with factory() as s:
        _seed_straddle(s)
        plan = sd.select_wings(s, _settings(), "sid-1")
    assert plan is not None
    assert plan["ce"].strike == 5400.0      # 5300 + 2×50
    assert plan["pe"].strike == 5200.0      # 5300 − 2×50
    assert plan["quantity"] == 5
    assert plan["exchange"] == "MCX"


def test_select_wings_fallback_when_exact_strike_missing():
    factory = _factory()
    with factory() as s:
        _seed_straddle(s, drop_ce_wing_at_steps=2)   # no CE at 5400
        plan = sd.select_wings(s, _settings(), "sid-1")
    assert plan is not None
    assert plan["ce"].strike == 5450.0      # steps+1 fallback
    assert plan["pe"].strike == 5200.0


def test_select_wings_none_when_straddle_closed():
    factory = _factory()
    with factory() as s:
        _seed_straddle(s)
        for pos in s.query(Position).all():
            s.add(ClosedTrade(position_id=pos.id, exchange="MCX",
                              tradingsymbol=pos.tradingsymbol, entry_premium=100.0,
                              exit_premium=90.0, pnl=50.0, exit_reason="MANUAL_EXIT",
                              opened_at=NOW, closed_at=NOW))
        s.commit()
        assert sd.select_wings(s, _settings(), "sid-1") is None


# ── proposals ─────────────────────────────────────────────────────────────────

def test_propose_hedge_creates_proposal_with_est_cost():
    factory = _factory()
    kite = MagicMock()
    kite.quote.side_effect = _quote_mock(40.0)
    sent: list[str] = []
    with factory() as s:
        _seed_straddle(s)
        with patch("app.commodity_agents.notify.send_telegram",
                   side_effect=lambda st_, t: sent.append(t) or True):
            action = sd.propose_hedge(s, _settings(), "sid-1", "manual", NOW,
                                      "MANUAL", kite=kite)
        s.commit()
        assert action is not None
        assert action.status == "PROPOSED"
        assert action.confirm_token
        # (40+40) ask × qty 5 × 10 units = ₹4,000
        assert action.est_cost == pytest.approx(4000.0)
        assert action.expires_at == NOW + timedelta(minutes=10)
    assert len(sent) == 1 and "wing hedge proposed" in sent[0]


def test_propose_hedge_dedups_existing_active():
    factory = _factory()
    kite = MagicMock()
    kite.quote.side_effect = _quote_mock()
    with factory() as s:
        _seed_straddle(s)
        s.add(HedgeAction(straddle_key="sid-1", underlying="CRUDEOILM", exchange="MCX",
                          mode="AUTO", trigger="reactive", status="ACTIVE",
                          ce_symbol="X", pe_symbol="Y", quantity=5, est_cost=1000.0,
                          proposed_at=NOW))
        s.commit()
        with patch("app.commodity_agents.notify.send_telegram", return_value=True):
            assert sd.propose_hedge(s, _settings(), "sid-1", "manual", NOW,
                                    "MANUAL", kite=kite) is None


def test_propose_hedge_respects_daily_budget_cap():
    factory = _factory()
    kite = MagicMock()
    kite.quote.side_effect = _quote_mock(40.0)   # est would be ₹4,000
    sent: list[str] = []
    with factory() as s:
        _seed_straddle(s)
        s.add(HedgeAction(straddle_key="sid-old", underlying="CRUDEOILM", exchange="MCX",
                          mode="AUTO", trigger="reactive", status="UNWOUND",
                          ce_symbol="X", pe_symbol="Y", quantity=5, est_cost=0.0,
                          entry_cost=3500.0, proposed_at=NOW - timedelta(hours=2)))
        s.commit()
        with patch("app.commodity_agents.notify.send_telegram",
                   side_effect=lambda st_, t: sent.append(t) or True):
            # 3500 spent + 4000 est > 6000 cap
            assert sd.propose_hedge(s, _settings(), "sid-1", "reactive", NOW,
                                    "SEMI_AUTO", kite=kite) is None
    assert any("budget cap" in t for t in sent)


def test_decide_hedge_reject_token_and_expiry(monkeypatch):
    import app.state as state
    monkeypatch.setattr(state, "is_paper_mode", lambda d: True)
    factory = _factory()
    kite = MagicMock()
    kite.quote.side_effect = _quote_mock()
    with factory() as s:
        _seed_straddle(s)
        with patch("app.commodity_agents.notify.send_telegram", return_value=True):
            action = sd.propose_hedge(s, _settings(), "sid-1", "manual", NOW,
                                      "MANUAL", kite=kite)
            s.commit()
            aid, token = action.id, action.confirm_token

            ok, msg = sd.decide_hedge(s, _settings(), aid, "wrong-token", True, NOW)
            assert not ok and "token" in msg

            ok, _ = sd.decide_hedge(s, _settings(), aid, token, False, NOW)
            assert ok and s.get(HedgeAction, aid).status == "REJECTED"

            # a fresh proposal that has already timed out expires on decide
            act2 = sd.propose_hedge(s, _settings(), "sid-1", "manual", NOW,
                                    "MANUAL", kite=kite)
            s.commit()
            late = NOW + timedelta(minutes=11)
            ok, msg = sd.decide_hedge(s, _settings(), act2.id, act2.confirm_token,
                                      True, late)
            assert not ok and "expired" in msg
            assert s.get(HedgeAction, act2.id).status == "EXPIRED"


# ── execution + unwind ────────────────────────────────────────────────────────

def _live_kite(monkeypatch):
    """Kite mock + live (non-paper) state for execution tests."""
    import app.state as state
    monkeypatch.setattr(state, "is_paper_mode", lambda d: False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    kite = MagicMock()
    kite.quote.side_effect = _quote_mock(40.0)
    kite.orders.return_value = [
        {"order_id": "OID-CE", "average_price": 42.0},
        {"order_id": "OID-PE", "average_price": 38.0},
    ]
    mgr = MagicMock()
    mgr.get_kite.return_value = kite
    return kite, mgr


def _place_entry_stub(kite_client, instrument, side, qty, order_type, product, **kw):
    return "OID-CE" if instrument.instrument_type == "CE" else "OID-PE"


def test_execute_hedge_places_wings_suspends_gtts(monkeypatch):
    kite, mgr = _live_kite(monkeypatch)
    factory = _factory()
    sent: list[str] = []
    with factory() as s:
        _seed_straddle(s)
        with patch("app.commodity_agents.notify.send_telegram", return_value=True):
            action = sd.propose_hedge(s, _settings(), "sid-1", "reactive", NOW,
                                      "SEMI_AUTO", kite=kite)
        s.commit()
        with (
            patch("app.kite_session.get_session_manager", return_value=mgr),
            patch("app.orders.place_entry", side_effect=_place_entry_stub) as pe,
            patch("app.orders.cancel_gtt", return_value=True) as cg,
            patch("app.commodity_agents.notify.send_telegram",
                  side_effect=lambda st_, t: sent.append(t) or True),
        ):
            assert sd.execute_hedge(s, _settings(), action, NOW) is True
        s.commit()

        assert pe.call_count == 2
        assert cg.call_count == 2                      # both short-leg GTTs cancelled
        assert action.status == "ACTIVE"
        # fills (42+38) × qty 5 × 10 units = ₹4,000
        assert action.entry_cost == pytest.approx(4000.0)
        assert {g.status for g in s.query(Gtt).all()} == {"SUSPENDED"}
        recs = json.loads(action.suspended_gtts)
        assert len(recs) == 2 and recs[0]["sl_trigger"] == 150.0

        wings = s.query(Position).filter(Position.current_sl == 0.0).all()
        assert len(wings) == 2                         # wing positions, no GTT
        wing_orders = s.query(Order).filter(Order.transaction_type == "BUY").all()
        assert all(o.status == "COMPLETE" for o in wing_orders)
    assert any("wings ON" in t for t in sent)


def test_execute_hedge_reverses_lone_leg_on_partial_failure(monkeypatch):
    kite, mgr = _live_kite(monkeypatch)
    factory = _factory()

    def _pe_fails(kite_client, instrument, side, qty, order_type, product, **kw):
        if instrument.instrument_type == "PE":
            raise RuntimeError("rejected")
        return "OID-CE"

    with factory() as s:
        _seed_straddle(s)
        with patch("app.commodity_agents.notify.send_telegram", return_value=True):
            action = sd.propose_hedge(s, _settings(), "sid-1", "reactive", NOW,
                                      "SEMI_AUTO", kite=kite)
        s.commit()
        with (
            patch("app.kite_session.get_session_manager", return_value=mgr),
            patch("app.orders.place_entry", side_effect=_pe_fails),
            patch("app.orders.square_off", return_value="REV-1") as so,
            patch("app.commodity_agents.notify.send_telegram", return_value=True),
        ):
            assert sd.execute_hedge(s, _settings(), action, NOW) is False
        assert action.status == "FAILED"
        so.assert_called_once()                        # lone CE wing reversed
        assert {g.status for g in s.query(Gtt).all()} == {"ACTIVE"}  # untouched


def test_unwind_hedge_closes_wings_and_restores_gtts(monkeypatch):
    kite, mgr = _live_kite(monkeypatch)
    factory = _factory()
    sent: list[str] = []
    with factory() as s:
        _seed_straddle(s)
        with patch("app.commodity_agents.notify.send_telegram", return_value=True):
            action = sd.propose_hedge(s, _settings(), "sid-1", "prehedge", NOW,
                                      "SEMI_AUTO", kite=kite)
        s.commit()
        with (
            patch("app.kite_session.get_session_manager", return_value=mgr),
            patch("app.orders.place_entry", side_effect=_place_entry_stub),
            patch("app.orders.cancel_gtt", return_value=True),
            patch("app.commodity_agents.notify.send_telegram", return_value=True),
        ):
            sd.execute_hedge(s, _settings(), action, NOW)
        s.commit()

        later = NOW + timedelta(hours=2)
        with (
            patch("app.kite_session.get_session_manager", return_value=mgr),
            patch("app.orders.square_off", return_value="X-1") as so,
            patch("app.orders.place_gtt_oco", return_value=7777) as pg,
            patch("app.commodity_agents.notify.send_telegram",
                  side_effect=lambda st_, t: sent.append(t) or True),
        ):
            assert sd.unwind_hedge(s, _settings(), action, later, reason="scheduled")
        s.commit()

        assert action.status == "UNWOUND"
        assert so.call_count == 2                      # both wings sold
        assert pg.call_count == 2                      # both stops re-placed
        closed = s.query(ClosedTrade).filter(
            ClosedTrade.exit_reason == "HEDGE_UNWIND").all()
        assert len(closed) == 2
        active = s.query(Gtt).filter(Gtt.status == "ACTIVE").all()
        assert len(active) == 2 and all(g.kite_gtt_id == 7777 for g in active)
        # short positions point at the restored stops again
        shorts = s.query(Position).filter(Position.current_sl == 150.0).all()
        assert all(p.gtt_id in {g.id for g in active} for p in shorts)
        # exit (40+40) × 5 × 10 = 4000 vs entry 4000 → pnl 0
        assert action.pnl == pytest.approx(0.0)
    assert any("wings OFF" in t for t in sent)


# ── mode gating + job wiring ──────────────────────────────────────────────────

def test_effective_mode_auto_requires_env_gate():
    assert sd.effective_mode(_settings(STRADDLE_DEFENSE_MODE="AUTO")) == "SEMI_AUTO"
    assert sd.effective_mode(_settings(STRADDLE_DEFENSE_MODE="AUTO",
                                       STRADDLE_DEFENSE_AUTO_EXECUTE=True)) == "AUTO"
    assert sd.effective_mode(_settings()) == "ALERT"


def test_job_semi_auto_reactive_creates_proposal(monkeypatch):
    import app.state as state
    monkeypatch.setattr(state, "is_straddle_defense_enabled", lambda d: True)
    monkeypatch.setattr(state, "is_paper_mode", lambda d: True)
    factory = _factory()
    sent: list[str] = []

    now_real = datetime.now(IST)
    kite = MagicMock()
    kite.quote.side_effect = _quote_mock(40.0)
    mgr = MagicMock()
    mgr.get_kite.return_value = kite
    with factory() as s:
        _seed_straddle(s)
        for i, iv in enumerate([30.0, 30.4, 30.9]):
            s.add(IvSnapshot(at=now_real - timedelta(minutes=3 - i),
                             underlying="CRUDEOILM", straddle_key="sid-1",
                             mtm=2000.0, iv_pct=iv))
        s.commit()
    st = sd._today_state(now_real)
    st["straddles"]["sid-1"] = {"peak": 2000.0, "alerts": 0, "last_alert": None}

    with (
        patch("app.kite_session.get_session_manager", return_value=mgr),
        patch("app.commodity_agents.portfolio.compute_portfolio_greeks",
              return_value=_mock_greeks(mtm=-3500.0, iv_pct=31.5)),
        patch("app.commodity_agents.notify.send_telegram",
              side_effect=lambda st_, t: sent.append(t) or True),
    ):
        sd.straddle_defense_job(_job_settings(STRADDLE_DEFENSE_MODE="SEMI_AUTO"), factory)

    with factory() as s:
        actions = s.query(HedgeAction).all()
        assert len(actions) == 1
        assert actions[0].status == "PROPOSED"
        assert actions[0].trigger == "reactive"
        assert actions[0].mode == "SEMI_AUTO"
    assert any("straddle defense" in t for t in sent)          # drawdown alert
    assert any("wing hedge proposed" in t for t in sent)       # proposal


def test_job_pauses_alerts_while_hedged(monkeypatch):
    import app.state as state
    monkeypatch.setattr(state, "is_straddle_defense_enabled", lambda d: True)
    factory = _factory()
    sent: list[str] = []

    now_real = datetime.now(IST)
    with factory() as s:
        for i, iv in enumerate([30.0, 30.4, 30.9]):
            s.add(IvSnapshot(at=now_real - timedelta(minutes=3 - i),
                             underlying="CRUDEOILM", straddle_key="sid-1",
                             mtm=2000.0, iv_pct=iv))
        s.add(HedgeAction(straddle_key="sid-1", underlying="CRUDEOILM", exchange="MCX",
                          mode="AUTO", trigger="reactive", status="ACTIVE",
                          ce_symbol="X", pe_symbol="Y", quantity=5, est_cost=1000.0,
                          proposed_at=now_real))
        s.commit()
    st = sd._today_state(now_real)
    st["straddles"]["sid-1"] = {"peak": 2000.0, "alerts": 0, "last_alert": None}

    with (
        patch("app.kite_session.get_session_manager", return_value=MagicMock()),
        patch("app.commodity_agents.portfolio.compute_portfolio_greeks",
              return_value=_mock_greeks(mtm=-3500.0, iv_pct=31.5)),
        patch("app.commodity_agents.notify.send_telegram",
              side_effect=lambda st_, t: sent.append(t) or True),
    ):
        sd.straddle_defense_job(_job_settings(), factory)

    assert sent == []                                  # defended → no alert spam
    with factory() as s:
        assert s.query(IvSnapshot).count() == 4        # sampling continues


def test_job_expires_stale_proposals(monkeypatch):
    import app.state as state
    monkeypatch.setattr(state, "is_straddle_defense_enabled", lambda d: True)
    factory = _factory()
    now_real = datetime.now(IST)
    with factory() as s:
        s.add(HedgeAction(straddle_key="sid-1", underlying="CRUDEOILM", exchange="MCX",
                          mode="SEMI_AUTO", trigger="reactive", status="PROPOSED",
                          ce_symbol="X", pe_symbol="Y", quantity=5, est_cost=1000.0,
                          proposed_at=now_real - timedelta(minutes=30),
                          expires_at=now_real - timedelta(minutes=20)))
        s.commit()
    with (
        patch("app.kite_session.get_session_manager", return_value=MagicMock()),
        patch("app.commodity_agents.portfolio.compute_portfolio_greeks",
              return_value={"positions": [], "straddles": {}, "totals": {}, "margins": None}),
        patch("app.commodity_agents.notify.send_telegram", return_value=True),
    ):
        sd.straddle_defense_job(_job_settings(), factory)
    with factory() as s:
        assert s.query(HedgeAction).one().status == "EXPIRED"


def test_scheduled_unwind_auto_and_force(monkeypatch):
    factory = _factory()
    unwound: list[tuple[int, str]] = []
    monkeypatch.setattr(sd, "unwind_hedge",
                        lambda s_, st_, a, now, reason="": unwound.append((a.id, reason)) or True)
    with factory() as s:
        s.add(HedgeAction(straddle_key="sid-1", underlying="CRUDEOILM", exchange="MCX",
                          mode="AUTO", trigger="prehedge", status="ACTIVE",
                          ce_symbol="X", pe_symbol="Y", quantity=5, est_cost=1000.0,
                          proposed_at=NOW))
        s.commit()

        # before the window: nothing
        early = NOW.replace(hour=19, minute=0)
        sd._maybe_unwind_scheduled(s, _settings(), {"straddles": {}}, early, "AUTO")
        assert unwound == []

        # inside the scheduled window, AUTO unwinds
        sched = NOW.replace(hour=20, minute=50)
        sd._maybe_unwind_scheduled(s, _settings(), {"straddles": {}}, sched, "AUTO")
        assert unwound == [(1, "scheduled")]

        # past the force cutoff every mode unwinds
        unwound.clear()
        late = NOW.replace(hour=23, minute=12)
        sd._maybe_unwind_scheduled(s, _settings(), {"straddles": {}}, late, "ALERT")
        assert unwound == [(1, "force_eod")]


def test_scheduled_unwind_semi_auto_nudges_once(monkeypatch):
    factory = _factory()
    sent: list[str] = []
    with factory() as s:
        s.add(HedgeAction(straddle_key="sid-1", underlying="CRUDEOILM", exchange="MCX",
                          mode="SEMI_AUTO", trigger="prehedge", status="ACTIVE",
                          ce_symbol="X", pe_symbol="Y", quantity=5, est_cost=1000.0,
                          proposed_at=NOW))
        s.commit()
        st = {"straddles": {}}
        sched = NOW.replace(hour=20, minute=50)
        with patch("app.commodity_agents.notify.send_telegram",
                   side_effect=lambda st_, t: sent.append(t) or True):
            sd._maybe_unwind_scheduled(s, _settings(), st, sched, "SEMI_AUTO")
            sd._maybe_unwind_scheduled(s, _settings(), st, sched, "SEMI_AUTO")
        assert s.query(HedgeAction).one().status == "ACTIVE"   # nudge only
    assert len(sent) == 1 and "Cool-off over" in sent[0]


def test_current_status_carries_hedge_info():
    factory = _factory()
    now_real = datetime.now(IST)
    with factory() as s:
        s.add(IvSnapshot(at=now_real, underlying="CRUDEOILM",
                         straddle_key="sid-1", mtm=-1000.0, iv_pct=33.0))
        s.add(HedgeAction(straddle_key="sid-1", underlying="CRUDEOILM", exchange="MCX",
                          mode="SEMI_AUTO", trigger="reactive", status="PROPOSED",
                          ce_symbol="C1", pe_symbol="P1", quantity=5, est_cost=4000.0,
                          confirm_token="tok", proposed_at=now_real,
                          expires_at=now_real + timedelta(minutes=10)))
        # orphan hedge on a straddle with no snapshot today
        s.add(HedgeAction(straddle_key="sid-2", underlying="NATURALGAS", exchange="MCX",
                          mode="AUTO", trigger="prehedge", status="ACTIVE",
                          ce_symbol="C2", pe_symbol="P2", quantity=1, est_cost=2000.0,
                          entry_cost=2100.0, proposed_at=now_real))
        s.commit()
        rows = sd.current_status(s, _settings())
    assert len(rows) == 2
    by_key = {r["key"]: r for r in rows}
    assert by_key["sid-1"]["hedge"]["status"] == "PROPOSED"
    assert by_key["sid-1"]["hedge"]["confirm_token"] == "tok"
    assert by_key["sid-2"]["hedge"]["status"] == "ACTIVE"
    assert by_key["sid-2"]["underlying"] == "NATURALGAS"
