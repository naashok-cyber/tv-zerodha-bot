"""Tests for app/main.py — all offline; Kite, DB, and lifespan fully controlled."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from cachetools import TTLCache
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.config import Settings
from app.main import app, get_current_settings, get_db_session
from app.storage import Alert, Base


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
