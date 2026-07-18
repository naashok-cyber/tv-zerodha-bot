"""Tests for app/main.py — all offline; Kite, DB, and lifespan fully controlled."""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, call, patch

import pytest
from cachetools import TTLCache
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.config import IST, Settings
from app.main import app, get_current_settings, get_db_session
from app.storage import Alert, AppError, Base, Gtt, Instrument, Order, Position
from app.watcher import EntryFilledEvent


# ── Shared test config ────────────────────────────────────────────────────────

_TV_IP = "52.89.214.238"   # first real TradingView IP
_SECRET = "test-hmac-secret"


def _test_settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        WEBHOOK_SECRET=_SECRET,
        DRY_RUN=True,
        TV_ALLOWED_IPS=[_TV_IP],
        DATABASE_URL="sqlite:///:memory:",
        DASHBOARD_PASSWORD="",       # open dashboard for smoke tests
        KITE_API_KEY="fake_key",
        SECRET_KEY="",               # no encryption needed in these tests
        PBKDF2_ITERATIONS=1,
        ENTRY_WINDOW_START="00:00",  # open all day so tests aren't time-of-day sensitive
        ENTRY_WINDOW_END="23:59",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_session_factory():
    """In-memory SQLite with StaticPool so route handler and background task share one DB."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _payload(**overrides) -> dict:
    base = {
        "symbol": "NIFTY",
        "action": "BUY",
        "price": 19500.0,
        "timeframe": "5",
        "alert_id": "tv_test_001",
        "timestamp": "2026-04-26T10:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def client(monkeypatch):
    """TestClient with in-memory DB, overridden settings, fresh idempotency cache."""
    factory = _make_session_factory()
    settings = _test_settings()

    monkeypatch.setattr(main_module, "_SessionFactory", factory)
    monkeypatch.setattr(main_module, "_idempotency_cache", TTLCache(maxsize=10_000, ttl=86_400))

    def _override_db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_current_settings] = lambda: settings

    with TestClient(app) as c:
        yield c, factory

    app.dependency_overrides.clear()


# ── Webhook: auth & validation ────────────────────────────────────────────────

def test_webhook_valid_payload_returns_202_and_persists_alert(client) -> None:
    c, factory = client
    resp = c.post(
        "/webhook",
        json=_payload(),
        headers={"X-Forwarded-For": _TV_IP},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"

    with factory() as session:
        rows = session.query(Alert).all()
    assert len(rows) == 1
    assert rows[0].tv_ticker == "NIFTY"
    assert rows[0].action == "BUY"


def test_webhook_invalid_price_returns_422(client) -> None:
    c, _ = client
    resp = c.post(
        "/webhook",
        json=_payload(price=0),
        headers={"X-Forwarded-For": _TV_IP},
    )
    assert resp.status_code == 422


def test_webhook_blocked_ip_returns_401(client) -> None:
    c, _ = client
    resp = c.post(
        "/webhook",
        json=_payload(),
        headers={"X-Forwarded-For": "9.9.9.9"},
    )
    assert resp.status_code == 401
    assert "allowlist" in resp.json()["detail"].lower()


def test_webhook_duplicate_returns_202_duplicate_one_db_row(client) -> None:
    c, factory = client
    payload = _payload()
    headers = {"X-Forwarded-For": _TV_IP}

    r1 = c.post("/webhook", json=payload, headers=headers)
    r2 = c.post("/webhook", json=payload, headers=headers)

    assert r1.status_code == 202
    assert r1.json()["status"] == "queued"
    assert r2.status_code == 202
    assert r2.json()["status"] == "duplicate"

    with factory() as session:
        rows = session.query(Alert).all()
    assert len(rows) == 1


def test_webhook_unknown_field_returns_422(client) -> None:
    c, _ = client
    resp = c.post(
        "/webhook",
        json=_payload(extra_unknown_field="oops"),
        headers={"X-Forwarded-For": _TV_IP},
    )
    assert resp.status_code == 422


# ── Webhook: background task routing ─────────────────────────────────────────

def test_webhook_naturalgas_routes_to_ng_branch(client, caplog) -> None:
    c, _ = client
    import logging
    with caplog.at_level(logging.INFO, logger="app.main"):
        resp = c.post(
            "/webhook",
            json=_payload(symbol="NATURALGAS", alert_id="ng_test_001"),
            headers={"X-Forwarded-For": _TV_IP},
        )
    assert resp.status_code == 202
    # NG branch is now wired; with no instruments in DB the expiry resolution fails gracefully
    assert any("NG" in r.message or "NATURALGAS" in r.message for r in caplog.records)


def test_webhook_dry_run_logs_dry_run_message(client, caplog) -> None:
    c, _ = client
    import logging
    with caplog.at_level(logging.INFO, logger="app.main"):
        resp = c.post(
            "/webhook",
            json=_payload(alert_id="dry_test_001"),
            headers={"X-Forwarded-For": _TV_IP},
        )
    assert resp.status_code == 202
    assert any("DRY_RUN" in r.message for r in caplog.records)


# ── Kite callback ─────────────────────────────────────────────────────────────

def test_kite_callback_success_returns_200_html(client) -> None:
    c, _ = client
    mock_mgr = MagicMock()
    mock_mgr.handle_callback.return_value = "fake_access_token"
    with patch("app.main.get_session_manager", return_value=mock_mgr):
        resp = c.get("/kite/callback?status=success&request_token=tok123")
    assert resp.status_code == 200
    assert "Login complete" in resp.text
    mock_mgr.handle_callback.assert_called_once_with("tok123")


def test_kite_callback_failure_returns_400(client) -> None:
    c, _ = client
    resp = c.get("/kite/callback?status=failed&request_token=")
    assert resp.status_code == 400


# ── healthz ───────────────────────────────────────────────────────────────────

def test_healthz_returns_expected_shape(client) -> None:
    c, _ = client
    with patch("app.main.get_session_manager") as mock_factory:
        mock_factory.return_value._load_token.return_value = None
        resp = c.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "token_age_hours" in body
    assert "dry_run" in body
    assert body["dry_run"] is True


# ── Dashboard smoke test ──────────────────────────────────────────────────────

def test_dashboard_returns_200(client) -> None:
    c, _ = client
    resp = c.get("/dashboard")
    assert resp.status_code == 200
    assert "Recent Alerts" in resp.text


def test_control_page_renders_dashboard_sections(client) -> None:
    """Phase-1 /control layout: annunciator strip, Today hero, live-positions
    card, schedule rail, collapsible risk-params drawer, positions JS."""
    c, _ = client
    with patch("app.main.get_session_manager") as mock_factory:
        mock_factory.return_value.get_token_info.return_value = {
            "is_valid": False, "age_hours": None, "reason": "no token",
        }
        resp = c.get("/control")
    assert resp.status_code == 200
    html = resp.text
    assert "class='strip'" in html            # annunciator strip
    assert "hero-pnl" in html                 # Today P&L hero
    assert "Open Positions" in html
    assert "id='pos-wrap'" in html
    assert "Today's Schedule" in html
    assert "class='rail'" in html
    assert "<details class='cfgd card'>" in html
    assert "/commodity-agents/portfolio-greeks" in html  # live-greeks fetch
    # merged nav links to the commodity-agents pages
    assert "/commodity-agents/dashboard" in html
    assert "/commodity-agents/desk" in html
    # phase 2: commodity cards, activity feed, live summary poll (no meta refresh)
    assert "id='ca-grid'" in html
    assert "id='feed'" in html
    assert "/api/control/summary" in html
    assert "http-equiv='refresh'" not in html
    # phase 3: performance tiles, equity chart, heatmap, sparkline seed
    assert "Performance &mdash; 90 days" in html
    assert "id='eq-chart'" in html
    assert "class='hm'" in html
    assert "window.__snaps=" in html


def test_control_summary_endpoint_shape(client) -> None:
    c, _ = client
    with patch("app.main.get_session_manager") as mock_factory:
        mock_factory.return_value.get_token_info.return_value = {
            "is_valid": True, "age_hours": 2.0, "reason": "",
        }
        resp = c.get("/api/control/summary")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("realized", "today_loss", "max_loss", "trades_today", "max_trades",
                "open_positions", "max_positions", "consec_losses", "consec_limit",
                "paper", "emergency_stop", "trade_mode", "session_valid", "next_label"):
        assert key in body
    assert body["session_valid"] is True
    assert body["paper"] is True


def test_health_returns_ok(client) -> None:
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── _on_entry_filled: GTT placement pipeline ──────────────────────────────────

def _seed_alert_order_instrument(factory, kite_order_id="ORD001",
                                  tradingsymbol="NIFTY2651923400CE", exchange="NFO"):
    """Insert Alert + Instrument + Order and return (alert_id, order_id)."""
    now = datetime.now(IST)
    with factory() as s:
        alert = Alert(
            received_at=now, strategy_id="t", tv_ticker="NIFTY1!", tv_exchange="",
            action="BUY", product="NRML", idempotency_key=f"k_{kite_order_id}",
            raw_payload="{}", processed=True,
        )
        s.add(alert)
        s.flush()
        instr = Instrument(
            instrument_token=99999, exchange_token=999,
            tradingsymbol=tradingsymbol, name="NIFTY",
            expiry=date(2026, 5, 19), strike=23400.0,
            tick_size=0.05, lot_size=75,
            instrument_type="CE", segment="NFO", exchange=exchange,
        )
        s.add(instr)
        order = Order(
            alert_id=alert.id, kite_order_id=kite_order_id,
            variety="regular", exchange=exchange, tradingsymbol=tradingsymbol,
            transaction_type="BUY", order_type="MARKET", product="NRML",
            quantity=75, status="PENDING", placed_at=now, updated_at=now, dry_run=False,
        )
        s.add(order)
        s.commit()
        return alert.id, order.id


def test_on_entry_filled_creates_position_and_gtt(monkeypatch) -> None:
    """Happy path: fill event → Position created, GTT placed on Kite, Gtt row ACTIVE."""
    factory = _make_session_factory()
    monkeypatch.setattr(main_module, "_SessionFactory", factory)
    _seed_alert_order_instrument(factory)

    mock_kite = MagicMock()
    mock_kite.place_gtt.return_value = {"trigger_id": 42}
    # LTP must be inside the GTT band (sl=210, target=480) to avoid immediate square-off.
    mock_kite.ltp.return_value = {"NFO:NIFTY2651923400CE": {"last_price": 300.0}}
    mock_mgr = MagicMock()
    mock_mgr.get_kite.return_value = mock_kite
    monkeypatch.setattr(main_module, "get_session_manager", lambda: mock_mgr)
    monkeypatch.setattr(main_module, "_pending_order_meta",
                        {"ORD001": {"instrument_type": "CE", "underlying": "NIFTY"}})

    settings = _test_settings(DRY_RUN=False, SL_PREMIUM_PCT=0.30, RR_RATIO=2.0)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    from app.main import _on_entry_filled
    _on_entry_filled(EntryFilledEvent(kite_order_id="ORD001", fill_price=300.0, fill_qty=75))

    with factory() as s:
        positions = s.query(Position).all()
        gtts = s.query(Gtt).all()
        order = s.query(Order).filter_by(kite_order_id="ORD001").first()

    assert len(positions) == 1, "Position must be created on fill"
    assert positions[0].entry_premium == pytest.approx(300.0)
    assert positions[0].current_sl == pytest.approx(210.0)   # 300 * (1 - 0.30)

    assert len(gtts) == 1, "Gtt row must be created"
    assert gtts[0].status == "ACTIVE"
    assert gtts[0].kite_gtt_id == 42
    assert gtts[0].sl_trigger == pytest.approx(210.0)
    assert gtts[0].target_trigger == pytest.approx(480.0)    # 300 + 2 * (300*0.30)

    assert order.status == "COMPLETE"
    assert mock_kite.place_gtt.call_count == 1


def test_on_entry_filled_gtt_failure_saves_position_and_logs_error(monkeypatch) -> None:
    """GTT placement fails → Position is still committed, AppError written, Gtt marked GTT_FAILED."""
    factory = _make_session_factory()
    monkeypatch.setattr(main_module, "_SessionFactory", factory)
    _seed_alert_order_instrument(factory, kite_order_id="ORD002")

    mock_kite = MagicMock()
    mock_kite.place_gtt.side_effect = Exception("Kite rejected GTT")
    # LTP must be inside the GTT band (sl=210, target=480) so the failure comes from
    # place_gtt, not the LTP pre-check triggering an immediate square-off.
    mock_kite.ltp.return_value = {"NFO:NIFTY2651923400CE": {"last_price": 300.0}}
    mock_mgr = MagicMock()
    mock_mgr.get_kite.return_value = mock_kite
    monkeypatch.setattr(main_module, "get_session_manager", lambda: mock_mgr)
    monkeypatch.setattr(main_module, "_pending_order_meta",
                        {"ORD002": {"instrument_type": "CE", "underlying": "NIFTY"}})

    settings = _test_settings(DRY_RUN=False, SL_PREMIUM_PCT=0.30, RR_RATIO=2.0)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    from app.main import _on_entry_filled
    _on_entry_filled(EntryFilledEvent(kite_order_id="ORD002", fill_price=300.0, fill_qty=75))

    with factory() as s:
        positions = s.query(Position).all()
        gtts = s.query(Gtt).all()
        errors = s.query(AppError).all()

    assert len(positions) == 1, "Position must be saved even when GTT placement fails"
    assert len(gtts) == 1, "Gtt row must be created (as audit trail)"
    assert gtts[0].status == "GTT_FAILED"
    assert gtts[0].kite_gtt_id is None

    assert len(errors) == 1, "AppError must be written so /status shows the failure"
    assert errors[0].error_type == "GttPlacementError"
    assert "Kite rejected GTT" in errors[0].message


def test_watch_order_registered_after_session_commit(monkeypatch) -> None:
    """watch_order must be called only after the Order row is committed.

    Before the fix, watch_order was called before session.commit(), so
    _on_entry_filled could not find the Order (race condition). This test
    verifies the fix: when watch_order fires, the Order is already visible
    in a fresh DB session.
    """
    factory = _make_session_factory()
    settings = _test_settings(DRY_RUN=False, ENTRY_WINDOW_START="00:00", ENTRY_WINDOW_END="23:59")
    monkeypatch.setattr(main_module, "_SessionFactory", factory)

    # Seed Alert + Instrument so resolve_expiry / select_strike can be mocked
    now = datetime.now(IST)
    with factory() as s:
        alert = Alert(
            received_at=now, strategy_id="t", tv_ticker="NIFTY1!", tv_exchange="",
            action="BUY", product="NRML", idempotency_key="race_test_key",
            raw_payload="{}", processed=False,
        )
        s.add(alert)
        s.commit()
        alert_id = alert.id

    committed_when_watched: list[bool] = []

    mock_watcher = MagicMock()
    def _check_committed(order_id: str, kite_fetcher=None) -> None:
        # Open a fresh session — exactly what _on_entry_filled does
        with factory() as s:
            row = s.query(Order).filter_by(kite_order_id=order_id).first()
            committed_when_watched.append(row is not None)
    mock_watcher.watch_order.side_effect = _check_committed
    monkeypatch.setattr(main_module, "_watcher", mock_watcher)

    # Mock Kite + expiry/strike chain
    from app.expiry_resolver import ResolvedExpiry
    from app.strike_selector import StrikeSelection

    mock_instr = MagicMock()
    mock_instr.lot_size = 75
    mock_instr.exchange = "NFO"
    mock_instr.tradingsymbol = "NIFTY2651923400CE"

    mock_selection = MagicMock()
    mock_selection.instrument = mock_instr
    mock_selection.option_ltp = 300.0

    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 23400.0}}
    mock_mgr = MagicMock()
    mock_mgr.get_token_info.return_value = {"is_valid": True, "age_hours": 1, "reason": None}
    mock_mgr.get_kite.return_value = mock_kite
    monkeypatch.setattr(main_module, "get_session_manager", lambda: mock_mgr)

    monkeypatch.setattr(main_module, "resolve_expiry",
                        lambda *a, **kw: ResolvedExpiry(date(2026, 5, 21), 7, "NEAREST_WEEKLY"))
    monkeypatch.setattr(main_module, "select_strike", lambda *a, **kw: mock_selection)
    monkeypatch.setattr(main_module, "place_entry", lambda *a, **kw: "RACE_ORD_001")
    monkeypatch.setattr(main_module, "risk", MagicMock(
        check_risk_gates=lambda *a, **kw: None,
        compute_futures_qty=lambda *a, **kw: 75,
        daily_loss_remaining=MagicMock(return_value=__import__("decimal").Decimal("10000")),
    ))

    from app.main import _process_alert
    from app.schemas import AlertPayload
    payload = AlertPayload(
        symbol="NIFTY1!", action="BUY", price=23400.0,
        timeframe="5", alert_id="race_test_key",
        timestamp=now,
    )
    _process_alert(alert_id, payload, settings)

    assert committed_when_watched, "watch_order was never called"
    assert all(committed_when_watched), (
        "watch_order fired before Order was committed — race condition still present"
    )
