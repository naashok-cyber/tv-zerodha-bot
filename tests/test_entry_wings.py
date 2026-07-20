"""Tests for app/entry_wings.py — defined-risk wings bought with the straddle."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import IST, Settings
from app.entry_wings import (
    ENTRY_TRIGGER,
    attach_entry_wings,
    place_entry_wings,
    plan_entry_wings,
    record_entry_wings,
    wings_too_expensive,
)
from app.storage import Base, HedgeAction, Instrument, Order, Position

NOW = datetime(2026, 7, 20, 21, 55, tzinfo=IST)
_EXPIRY = datetime(2026, 7, 24).date()


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        KITE_API_KEY="fake",
        SECRET_KEY="",
        DASHBOARD_PASSWORD="",
        PBKDF2_ITERATIONS=1,
        STRADDLE_ENTRY_WINGS_ENABLED=True,
        STRADDLE_ENTRY_WING_STEPS=2,
        STRADDLE_ENTRY_WING_MAX_COST_PCT=0.35,
        STRADDLE_ENTRY_WINGS_REQUIRED=False,
        STRADDLE_FILL_TIMEOUT_SECS=5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_chain(session, underlying="CRUDEOILM", strike=5300.0, interval=50.0,
                drop_ce_at_steps: int | None = None) -> None:
    tok = [2000]

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
        if drop_ce_at_steps is None or i != drop_ce_at_steps:
            session.add(_inst("CE", k))
        session.add(_inst("PE", k))
    session.commit()


def _quote_mock(px: float = 40.0):
    def _quote(keys):
        return {k: {"last_price": px - 1,
                    "depth": {"sell": [{"price": px}], "buy": [{"price": px - 2}]}}
                for k in keys}
    return _quote


def _short_symbols(underlying="CRUDEOILM", strike=5300.0):
    return f"{underlying}26JUL{int(strike)}CE", f"{underlying}26JUL{int(strike)}PE"


# ── plan_entry_wings ──────────────────────────────────────────────────────────

def test_plan_entry_wings_picks_otm_strikes():
    factory = _factory()
    kite = MagicMock()
    kite.quote.side_effect = _quote_mock(40.0)
    ce_sym, pe_sym = _short_symbols()
    with factory() as s:
        _seed_chain(s)
        plan = plan_entry_wings(
            s, _settings(), underlying="CRUDEOILM", exchange="MCX",
            ce_short_symbol=ce_sym, pe_short_symbol=pe_sym, quantity=5, kite=kite,
        )
    assert plan is not None
    assert plan["ce"].strike == 5400.0   # 5300 + 2×50
    assert plan["pe"].strike == 5200.0   # 5300 − 2×50
    assert plan["cost"] == pytest.approx((40.0 + 40.0) * 5 * 10)   # MCX_LOT_UNITS CRUDEOILM=10


def test_plan_entry_wings_none_when_chain_too_thin():
    factory = _factory()
    ce_sym, pe_sym = _short_symbols()
    with factory() as s:
        _seed_chain(s, drop_ce_at_steps=2)
        # fallback tries steps+1 (3) and steps-1 (1); drop those too so nothing resolves
        s.query(Instrument).filter(
            Instrument.strike.in_([5450.0, 5350.0]), Instrument.instrument_type == "CE",
        ).delete(synchronize_session=False)
        s.commit()
        plan = plan_entry_wings(
            s, _settings(), underlying="CRUDEOILM", exchange="MCX",
            ce_short_symbol=ce_sym, pe_short_symbol=pe_sym, quantity=5, kite=None,
        )
    assert plan is None


def test_wings_too_expensive_over_cap():
    settings = _settings(STRADDLE_ENTRY_WING_MAX_COST_PCT=0.10)
    plan = {"cost": 500.0, "credit": 4000.0, "cost_pct": 500.0 / 4000.0}
    assert wings_too_expensive(settings, plan) is True


def test_wings_too_expensive_within_cap():
    settings = _settings(STRADDLE_ENTRY_WING_MAX_COST_PCT=0.35)
    plan = {"cost": 500.0, "credit": 4000.0, "cost_pct": 500.0 / 4000.0}
    assert wings_too_expensive(settings, plan) is False


def test_wings_too_expensive_zero_credit_never_blocks():
    settings = _settings()
    plan = {"cost": 500.0, "credit": 0.0, "cost_pct": 0.0}
    assert wings_too_expensive(settings, plan) is False


# ── place_entry_wings ─────────────────────────────────────────────────────────

def _fake_plan(qty=5):
    ce = Instrument(instrument_token=1, exchange_token=1, tradingsymbol="CE_WING",
                     name="CRUDEOILM", expiry=_EXPIRY, strike=5400.0, tick_size=0.1,
                     lot_size=1, instrument_type="CE", segment="MCX-OPT", exchange="MCX")
    pe = Instrument(instrument_token=2, exchange_token=2, tradingsymbol="PE_WING",
                     name="CRUDEOILM", expiry=_EXPIRY, strike=5200.0, tick_size=0.1,
                     lot_size=1, instrument_type="PE", segment="MCX-OPT", exchange="MCX")
    return {"ce": ce, "pe": pe, "underlying": "CRUDEOILM", "exchange": "MCX",
            "quantity": qty, "cost": 400.0, "credit": 4000.0, "cost_pct": 0.10}


def test_place_entry_wings_both_fill():
    kite = MagicMock()
    with patch("app.orders.place_entry", side_effect=["OID_CE", "OID_PE"]):
        result = place_entry_wings(_settings(), _fake_plan(), kite, "NRML")
    assert result == {"ce_order_id": "OID_CE", "pe_order_id": "OID_PE", "error": None}


def test_place_entry_wings_reverses_lone_leg_on_partial_failure():
    kite = MagicMock()
    reversed_calls = []
    with patch("app.orders.place_entry", side_effect=["OID_CE", Exception("PE rejected")]), \
         patch("app.orders.square_off", side_effect=lambda *a, **k: reversed_calls.append(a)):
        result = place_entry_wings(_settings(), _fake_plan(), kite, "NRML")
    assert result["ce_order_id"] is None and result["pe_order_id"] is None
    assert len(reversed_calls) == 1   # the filled CE wing was reversed


def test_place_entry_wings_both_fail_no_reversal_needed():
    kite = MagicMock()
    with patch("app.orders.place_entry", side_effect=[Exception("CE rejected"), Exception("PE rejected")]), \
         patch("app.orders.square_off") as sq:
        result = place_entry_wings(_settings(), _fake_plan(), kite, "NRML")
    assert result["ce_order_id"] is None and result["pe_order_id"] is None
    sq.assert_not_called()


# ── attach_entry_wings: required vs optional ─────────────────────────────────

def test_attach_entry_wings_optional_continues_when_unavailable():
    factory = _factory()
    ce_sym, pe_sym = _short_symbols()
    with factory() as s:
        # no chain seeded — plan_entry_wings returns None
        plan, blocked = attach_entry_wings(
            s, _settings(STRADDLE_ENTRY_WINGS_REQUIRED=False),
            underlying="CRUDEOILM", exchange="MCX",
            ce_short_symbol=ce_sym, pe_short_symbol=pe_sym,
            quantity=5, product="NRML", kite=None, dry=False,
        )
    assert plan is None
    assert blocked is None   # not required → straddle proceeds without wings


def test_attach_entry_wings_required_blocks_when_unavailable():
    factory = _factory()
    ce_sym, pe_sym = _short_symbols()
    with factory() as s:
        plan, blocked = attach_entry_wings(
            s, _settings(STRADDLE_ENTRY_WINGS_REQUIRED=True),
            underlying="CRUDEOILM", exchange="MCX",
            ce_short_symbol=ce_sym, pe_short_symbol=pe_sym,
            quantity=5, product="NRML", kite=None, dry=False,
        )
    assert plan is None
    assert blocked is not None   # required → straddle must be abandoned


def test_attach_entry_wings_dry_run_simulates_without_orders():
    factory = _factory()
    ce_sym, pe_sym = _short_symbols()
    with factory() as s:
        _seed_chain(s)
        plan, blocked = attach_entry_wings(
            s, _settings(), underlying="CRUDEOILM", exchange="MCX",
            ce_short_symbol=ce_sym, pe_short_symbol=pe_sym,
            quantity=5, product="NRML", kite=None, dry=True,
        )
    assert blocked is None
    assert plan is not None
    assert "orders" not in plan   # paper mode never calls place_entry_wings


def _tiered_quote_mock():
    """OTM wings (strikes 5200/5400) price cheap; ATM shorts (5300) price rich —
    like a real chain, so the cost-vs-credit cap has something realistic to check."""
    def _quote(keys):
        out = {}
        for k in keys:
            px = 10.0 if ("5400" in k or "5200" in k) else 60.0
            out[k] = {"last_price": px - 1,
                      "depth": {"sell": [{"price": px}], "buy": [{"price": px - 2}]}}
        return out
    return _quote


def test_attach_entry_wings_live_places_and_records_orders():
    factory = _factory()
    kite = MagicMock()
    kite.quote.side_effect = _tiered_quote_mock()
    ce_sym, pe_sym = _short_symbols()
    with factory() as s:
        _seed_chain(s)
        with patch("app.orders.place_entry", side_effect=["OID_CE", "OID_PE"]):
            plan, blocked = attach_entry_wings(
                s, _settings(), underlying="CRUDEOILM", exchange="MCX",
                ce_short_symbol=ce_sym, pe_short_symbol=pe_sym,
                quantity=5, product="NRML", kite=kite, dry=False,
            )
    assert blocked is None
    assert plan is not None
    assert plan["orders"] == {"ce_order_id": "OID_CE", "pe_order_id": "OID_PE", "error": None}


# ── record_entry_wings ────────────────────────────────────────────────────────

def test_record_entry_wings_creates_active_hedge_action():
    factory = _factory()
    with factory() as s:
        plan = _fake_plan(qty=5)
        orders = {"ce_order_id": "OID_CE", "pe_order_id": "OID_PE"}
        action = record_entry_wings(
            s, _settings(), plan, orders,
            straddle_key="sid-entry-1", now=NOW, dry=False, product="NRML",
        )
        s.commit()
        assert action.status == "ACTIVE"
        assert action.trigger == ENTRY_TRIGGER
        assert action.straddle_key == "sid-entry-1"
        assert action.ce_order_id == "OID_CE" and action.pe_order_id == "OID_PE"

        orders_written = s.query(Order).filter(Order.straddle_id == "sid-entry-1").all()
        assert len(orders_written) == 2
        assert {o.transaction_type for o in orders_written} == {"BUY"}

        positions_written = s.query(Position).all()
        assert len(positions_written) == 2
        # wings carry no protective GTT — bounded loss is the premium itself
        assert all(p.gtt_id is None for p in positions_written)


def test_record_entry_wings_paper_mode_marks_dry_run():
    factory = _factory()
    with factory() as s:
        plan = _fake_plan(qty=5)
        action = record_entry_wings(
            s, _settings(), plan, {}, straddle_key="sid-entry-2",
            now=NOW, dry=True, product="NRML",
        )
        s.commit()
        assert action.dry_run is True
        orders_written = s.query(Order).filter(Order.straddle_id == "sid-entry-2").all()
        assert all(o.dry_run for o in orders_written)
        assert all(o.status == "DRY_RUN" for o in orders_written)
