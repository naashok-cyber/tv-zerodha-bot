"""Tests for app/schemas.py — AlertPayload validation, and webhook endpoint contract."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from cachetools import TTLCache
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.config import IST, UTC
from app.main import app, get_current_settings, get_db_session
from app.schemas import AlertPayload
from app.config import Settings
from app.storage import Base


# ── AlertPayload unit tests ────────────────────────────────────────────────────

_TS_UTC = datetime(2026, 4, 27, 4, 30, 0, tzinfo=UTC)   # 04:30 UTC = 10:00 IST


def _valid(**overrides) -> dict:
    base = dict(
        symbol="NIFTY",
        action="BUY",
        price=Decimal("19500"),
        timeframe="5",
        alert_id="alert_001",
        timestamp=_TS_UTC,
    )
    base.update(overrides)
    return base


class TestAlertPayloadSchema:
    def test_valid_buy(self):
        p = AlertPayload(**_valid(action="BUY"))
        assert p.symbol == "NIFTY"
        assert p.action == "BUY"
        assert p.price == Decimal("19500")

    def test_valid_sell(self):
        p = AlertPayload(**_valid(action="SELL"))
        assert p.action == "SELL"

    def test_valid_exit(self):
        p = AlertPayload(**_valid(action="EXIT"))
        assert p.action == "EXIT"

    def test_valid_trail_with_premium(self):
        p = AlertPayload(**_valid(action="TRAIL", premium=Decimal("130")))
        assert p.premium == Decimal("130")

    def test_trail_without_premium_raises(self):
        with pytest.raises(ValidationError, match="premium is required"):
            AlertPayload(**_valid(action="TRAIL"))

    def test_price_zero_raises(self):
        with pytest.raises(ValidationError, match="price must be > 0"):
            AlertPayload(**_valid(price=Decimal("0")))

    def test_price_negative_raises(self):
        with pytest.raises(ValidationError, match="price must be > 0"):
            AlertPayload(**_valid(price=Decimal("-1")))

    def test_symbol_empty_raises(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            AlertPayload(**_valid(symbol=""))

    def test_symbol_too_long_raises(self):
        with pytest.raises(ValidationError, match="at most 20 characters"):
            AlertPayload(**_valid(symbol="A" * 21))

    def test_symbol_uppercased(self):
        p = AlertPayload(**_valid(symbol="nifty"))
        assert p.symbol == "NIFTY"

    def test_alert_id_whitespace_raises(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            AlertPayload(**_valid(alert_id="   "))

    def test_timestamp_naive_raises(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            AlertPayload(**_valid(timestamp=datetime(2026, 4, 27, 10, 0, 0)))

    def test_timestamp_utc_converted_to_ist(self):
        p = AlertPayload(**_valid(timestamp=_TS_UTC))
        assert p.timestamp.tzinfo is not None
        assert p.timestamp.strftime("%Z") in ("IST", "+0530")
        assert p.timestamp.hour == 10  # 04:30 UTC → 10:00 IST

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            AlertPayload(**_valid(secret="xyz"))


# ── Webhook endpoint contract tests ───────────────────────────────────────────

_TV_IP = "52.89.214.238"


def _make_settings(**kw) -> Settings:
    defaults = dict(
        _env_file=None,
        DRY_RUN=True,
        TV_ALLOWED_IPS=[_TV_IP],
        DATABASE_URL="sqlite:///:memory:",
        DASHBOARD_PASSWORD="",
        KITE_API_KEY="testkey",
        SECRET_KEY="",
        PBKDF2_ITERATIONS=1,
    )
    defaults.update(kw)
    return Settings(**defaults)


def _make_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def wclient(monkeypatch):
    factory = _make_factory()
    settings = _make_settings()
    monkeypatch.setattr(main_module, "_SessionFactory", factory)
    monkeypatch.setattr(main_module, "_idempotency_cache", TTLCache(maxsize=1000, ttl=3600))

    def _db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_current_settings] = lambda: settings

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _wpayload(**overrides) -> dict:
    base = dict(
        symbol="NIFTY",
        action="BUY",
        price=19500.0,
        timeframe="5",
        alert_id="schema_test_001",
        timestamp="2026-04-27T04:30:00+00:00",
    )
    base.update(overrides)
    return base


class TestWebhookEndpoint:
    def test_valid_buy_payload_returns_202(self, wclient):
        resp = wclient.post("/webhook", json=_wpayload(), headers={"X-Forwarded-For": _TV_IP})
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"

    def test_invalid_payload_missing_symbol_returns_422(self, wclient):
        body = _wpayload()
        del body["symbol"]
        resp = wclient.post("/webhook", json=body, headers={"X-Forwarded-For": _TV_IP})
        assert resp.status_code == 422

    def test_invalid_payload_price_zero_returns_422(self, wclient):
        resp = wclient.post(
            "/webhook",
            json=_wpayload(price=0),
            headers={"X-Forwarded-For": _TV_IP},
        )
        assert resp.status_code == 422

    def test_invalid_payload_unknown_field_returns_422(self, wclient):
        resp = wclient.post(
            "/webhook",
            json=_wpayload(secret="old_format"),
            headers={"X-Forwarded-For": _TV_IP},
        )
        assert resp.status_code == 422

    def test_blocked_ip_returns_401(self, wclient):
        resp = wclient.post(
            "/webhook",
            json=_wpayload(),
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        assert resp.status_code == 401
