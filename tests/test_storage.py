"""Tests for app/storage.py — all offline, in-memory SQLite, no network."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.storage import (
    Alert,
    AppError,
    Base,
    ClosedTrade,
    Gtt,
    KiteSession,
    Order,
    Position,
    StrikeDecision,
    init_db,
)

NOW = datetime.now(timezone.utc)

EXPECTED_TABLES = {
    "alerts",
    "orders",
    "gtts",
    "positions",
    "closed_trades",
    "sessions",
    "strike_decisions",
    "errors",
    "instruments",
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


# ── Helpers ───────────────────────────────────────────────────────────────────

def _alert(idempotency_key: str = "key-001") -> Alert:
    return Alert(
        received_at=NOW,
        strategy_id="ema_cross_v1",
        tv_ticker="NSE:NIFTY",
        tv_exchange="NSE",
        action="BUY",
        order_type="MARKET",
        product="NRML",
        idempotency_key=idempotency_key,
        raw_payload='{"action":"BUY"}',
    )


def _order(alert_id: int, status: str = "PENDING") -> Order:
    return Order(
        alert_id=alert_id,
        variety="regular",
        exchange="NFO",
        tradingsymbol="NIFTY26APR25000CE",
        transaction_type="BUY",
        order_type="MARKET",
        product="NRML",
        quantity=50,
        placed_at=NOW,
        updated_at=NOW,
        dry_run=True,
        status=status,
    )


def _position(order_id: int) -> Position:
    return Position(
        order_id=order_id,
        exchange="NFO",
        tradingsymbol="NIFTY26APR25000CE",
        underlying="NIFTY",
        instrument_type="CE",
        entry_premium=100.0,
        current_sl=70.0,   # 30% SL: 100 × 0.70 = 70
        quantity=50,
        lot_size=50,
        opened_at=NOW,
        last_updated_at=NOW,
    )


# ── Schema ────────────────────────────────────────────────────────────────────

def test_all_tables_created(engine):
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES


# ── Alert ─────────────────────────────────────────────────────────────────────

def test_alert_insert(session):
    a = _alert()
    session.add(a)
    session.commit()
    assert a.id is not None
    assert a.processed is False


def test_alert_idempotency_key_unique(session):
    session.add(_alert("dup-key"))
    session.commit()
    with pytest.raises(IntegrityError):
        session.add(_alert("dup-key"))
        session.commit()


def test_alert_different_keys_allowed(session):
    session.add(_alert("key-a"))
    session.add(_alert("key-b"))
    session.commit()  # both should succeed


def test_alert_nullable_fields(session):
    a = _alert()
    session.add(a)
    session.commit()
    assert a.entry_price is None
    assert a.stop_loss is None
    assert a.atr is None
    assert a.quantity_hint is None


# ── Order ─────────────────────────────────────────────────────────────────────

def test_order_insert(session):
    a = _alert()
    session.add(a)
    session.flush()
    o = _order(a.id)
    session.add(o)
    session.commit()
    assert o.id is not None
    assert o.dry_run is True
    assert o.kite_order_id is None


def test_order_fk_to_alert(session):
    a = _alert()
    session.add(a)
    session.flush()
    o = _order(a.id)
    session.add(o)
    session.commit()
    assert o.alert_id == a.id


# ── GTT ───────────────────────────────────────────────────────────────────────

def test_gtt_insert(session):
    a = _alert()
    session.add(a)
    session.flush()
    o = _order(a.id, status="COMPLETE")
    session.add(o)
    session.flush()
    gtt = Gtt(
        order_id=o.id,
        gtt_type="OCO",
        exchange="NFO",
        tradingsymbol="NIFTY26APR25000CE",
        sl_trigger=69.0,
        target_trigger=160.0,
        sl_order_price=68.5,
        target_order_price=159.5,
        last_price_at_placement=100.0,
        placed_at=NOW,
        updated_at=NOW,
        dry_run=True,
        status="ACTIVE",
    )
    session.add(gtt)
    session.commit()
    assert gtt.id is not None
    assert gtt.modification_count == 0


# ── Position ──────────────────────────────────────────────────────────────────

def test_position_insert(session):
    a = _alert()
    session.add(a)
    session.flush()
    o = _order(a.id, status="COMPLETE")
    session.add(o)
    session.flush()
    p = _position(o.id)
    session.add(p)
    session.commit()
    assert p.id is not None
    assert p.breakeven_moved is False
    assert p.trail_active is False
    assert p.gtt_id is None


def test_position_order_id_unique(session):
    """One entry order maps to at most one position."""
    a = _alert()
    session.add(a)
    session.flush()
    o = _order(a.id, status="COMPLETE")
    session.add(o)
    session.flush()
    session.add(_position(o.id))
    session.commit()
    with pytest.raises(IntegrityError):
        session.add(_position(o.id))
        session.commit()


def test_position_breakeven_update(session):
    a = _alert()
    session.add(a)
    session.flush()
    o = _order(a.id, status="COMPLETE")
    session.add(o)
    session.flush()
    p = _position(o.id)
    session.add(p)
    session.commit()

    p.breakeven_moved = True
    p.current_sl = 100.0   # SL moved to entry
    session.commit()

    session.expire(p)
    fetched = session.get(Position, p.id)
    assert fetched.breakeven_moved is True
    assert fetched.current_sl == pytest.approx(100.0)


# ── Closed Trade ──────────────────────────────────────────────────────────────

def test_closed_trade_insert(session):
    a = _alert()
    session.add(a)
    session.flush()
    o = _order(a.id, status="COMPLETE")
    session.add(o)
    session.flush()
    p = _position(o.id)
    session.add(p)
    session.flush()
    ct = ClosedTrade(
        position_id=p.id,
        exchange="NFO",
        tradingsymbol="NIFTY26APR25000CE",
        entry_premium=100.0,
        exit_premium=200.0,
        pnl=5_000.0,   # (200 - 100) × 50 lots
        exit_reason="TARGET_HIT",
        opened_at=NOW,
        closed_at=NOW,
    )
    session.add(ct)
    session.commit()
    assert ct.id is not None


# ── KiteSession ───────────────────────────────────────────────────────────────

def test_session_insert(session):
    ks = KiteSession(
        access_token="encrypted_ciphertext_placeholder",
        created_at=NOW,
        expires_at=NOW,
        is_active=True,
    )
    session.add(ks)
    session.commit()
    assert ks.id is not None
    assert ks.is_active is True


# ── StrikeDecision ────────────────────────────────────────────────────────────

def test_strike_decision_insert(session):
    a = _alert()
    session.add(a)
    session.flush()
    candidates = [
        {"strike": 25000, "delta": 0.63, "iv": 0.15, "ltp": 120.0, "oi": 5000},
        {"strike": 24950, "delta": 0.67, "iv": 0.14, "ltp": 135.0, "oi": 4500},
    ]
    sd = StrikeDecision(
        alert_id=a.id,
        underlying="NIFTY",
        expiry="2026-05-01",
        flag="c",
        target_delta=0.65,
        candidates_json=json.dumps(candidates),
        selected_tradingsymbol="NIFTY26MAY25000CE",
        selected_strike=25000.0,
        selected_delta=0.63,
        selected_iv=0.15,
        selected_ltp=120.0,
        decided_at=NOW,
    )
    session.add(sd)
    session.commit()
    assert sd.id is not None
    assert sd.rejection_reason is None


def test_strike_decision_rejection(session):
    a = _alert()
    session.add(a)
    session.flush()
    sd = StrikeDecision(
        alert_id=a.id,
        underlying="NIFTY",
        expiry="2026-05-01",
        flag="c",
        target_delta=0.65,
        candidates_json=json.dumps([]),
        rejection_reason="No valid strike found: all candidates below MIN_OI_INDEX",
        decided_at=NOW,
    )
    session.add(sd)
    session.commit()
    assert sd.selected_tradingsymbol is None
    assert "MIN_OI_INDEX" in sd.rejection_reason


# ── AppError ──────────────────────────────────────────────────────────────────

def test_error_insert_without_alert(session):
    err = AppError(
        error_type="TokenException",
        message="Access token expired",
        traceback="Traceback (most recent call last): ...",
        occurred_at=NOW,
    )
    session.add(err)
    session.commit()
    assert err.id is not None
    assert err.alert_id is None


def test_error_insert_with_alert(session):
    a = _alert()
    session.add(a)
    session.flush()
    err = AppError(
        alert_id=a.id,
        error_type="NetworkException",
        message="Connection reset",
        occurred_at=NOW,
    )
    session.add(err)
    session.commit()
    assert err.alert_id == a.id


# ── Timestamps ────────────────────────────────────────────────────────────────

def test_utc_timestamp_stored_and_retrieved(session):
    a = _alert()
    session.add(a)
    session.commit()
    session.expire(a)
    fetched = session.get(Alert, a.id)
    assert fetched.received_at is not None
    assert fetched.received_at.year == NOW.year


# ── init_db ───────────────────────────────────────────────────────────────────

def test_init_db_in_memory():
    engine = init_db(database_url="sqlite:///:memory:")
    names = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES == names
    engine.dispose()


def test_init_db_creates_data_dir(tmp_path):
    db_file = tmp_path / "sub" / "bot.db"
    engine = init_db(database_url=f"sqlite:///{db_file}")
    assert db_file.exists()
    names = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES == names
    engine.dispose()
