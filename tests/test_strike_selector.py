"""Tests for app/strike_selector.py — all offline, no network, Kite fully mocked."""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import IST, Settings
from app.greeks import GreeksResult
from app.storage import Base, Instrument, StrikeDecision
from app.strike_selector import NoValidStrikeError, StrikeSelection, select_strike


# ── Settings helper ───────────────────────────────────────────────────────────

def _s(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


# ── DB fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ── Instrument / quote helpers ────────────────────────────────────────────────

_EXPIRY = date(2025, 1, 16)
_SPOT = 23_500.0
_TODAY_NOW = datetime(2025, 1, 9, 10, 0, tzinfo=IST)   # 7 days before expiry

# 10 CE strikes centred around spot
_STRIKES = [22_500.0, 22_750.0, 23_000.0, 23_250.0, 23_500.0,
            23_750.0, 24_000.0, 24_250.0, 24_500.0, 24_750.0]

# Synthetic deltas (higher strike → lower call delta)
_DELTAS: dict[float, float] = {
    22_500.0: 0.90,
    22_750.0: 0.82,
    23_000.0: 0.74,
    23_250.0: 0.67,
    23_500.0: 0.58,
    23_750.0: 0.49,
    24_000.0: 0.40,
    24_250.0: 0.31,
    24_500.0: 0.23,
    24_750.0: 0.16,
}
_IV = 0.15
_LTP: dict[float, float] = {s: max(5.1, 500 - (s - 22_500) * 0.18) for s in _STRIKES}


def _make_inst(strike: float, token: int, itype: str = "CE", segment: str = "NFO-OPT") -> Instrument:
    inst = Instrument()
    inst.instrument_token = token
    inst.exchange_token = token
    inst.tradingsymbol = f"NIFTY25116{int(strike)}{itype}"
    inst.name = "NIFTY"
    inst.expiry = _EXPIRY
    inst.strike = strike
    inst.tick_size = 0.05
    inst.lot_size = 75
    inst.instrument_type = itype
    inst.segment = segment
    inst.exchange = "NFO"
    return inst


def _populate_chain(session: Session, itype: str = "CE") -> list[Instrument]:
    insts = [_make_inst(s, 1000 + i, itype) for i, s in enumerate(_STRIKES)]
    for inst in insts:
        session.add(inst)
    session.commit()
    return insts


def _make_quotes(insts: list[Instrument], ltps: dict[float, float] | None = None) -> dict:
    """Build a fake kite.quote() response."""
    result = {}
    for inst in insts:
        ltp = (ltps or _LTP).get(inst.strike, 100.0)
        result[f"NFO:{inst.tradingsymbol}"] = {
            "last_price": ltp,
            "oi": 5_000,
            "depth": {
                "buy":  [{"price": ltp - 0.5, "quantity": 100, "orders": 5}],
                "sell": [{"price": ltp + 0.5, "quantity": 100, "orders": 5}],
            },
        }
    return result


def _mock_compute_delta(instrument, option_ltp, underlying, t):
    """Return a synthetic GreeksResult keyed by the instrument's strike."""
    d = _DELTAS.get(instrument.strike, 0.50)
    return GreeksResult(delta=d, iv=_IV, model_used="BSM", rejection_reason=None)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_happy_path_picks_closest_to_target(session, monkeypatch):
    insts = _populate_chain(session)
    kite = MagicMock()
    kite.quote.return_value = _make_quotes(insts)
    monkeypatch.setattr("app.strike_selector.compute_delta", _mock_compute_delta)

    result = select_strike(
        "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
        segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
        now=_TODAY_NOW, settings=_s(),
    )

    # closest to 0.65 is strike 23_250 (delta 0.67, distance 0.02)
    # 23_500 has delta 0.58 (distance 0.07 > tol 0.05 from 23_250's 0.67)
    # Actually: |0.67 - 0.65| = 0.02, |0.58 - 0.65| = 0.07 → 23_250 wins
    assert result.instrument.strike == 23_250.0
    assert abs(result.computed_delta - 0.67) < 0.001
    assert result.candidates_considered == len(_STRIKES)


def test_default_target_delta_0_65(session, monkeypatch):
    insts = _populate_chain(session)
    kite = MagicMock()
    kite.quote.return_value = _make_quotes(insts)
    monkeypatch.setattr("app.strike_selector.compute_delta", _mock_compute_delta)

    # Do not pass target_delta — it should read TARGET_DELTA=0.65 from config.
    result = select_strike(
        "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
        segment="NFO-OPT", now=_TODAY_NOW, settings=_s(TARGET_DELTA=0.65),
    )
    assert result.instrument.strike == 23_250.0


# ── Tie-breaker ───────────────────────────────────────────────────────────────

def test_tiebreaker_prefers_itm(session, monkeypatch):
    # Two strikes equidistant from 0.65: delta=0.64 and delta=0.66.
    # ITM = higher abs-delta → 0.66 wins.
    insts = [
        _make_inst(23_000.0, 2001),
        _make_inst(23_500.0, 2002),
    ]
    for i in insts:
        session.add(i)
    session.commit()

    tied_deltas = {23_000.0: 0.66, 23_500.0: 0.64}

    def _tied(instrument, ltp, underlying, t):
        return GreeksResult(delta=tied_deltas[instrument.strike], iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = _make_quotes(insts)
    monkeypatch.setattr("app.strike_selector.compute_delta", _tied)

    result = select_strike(
        "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
        segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
        now=_TODAY_NOW, settings=_s(),
    )
    assert result.instrument.strike == 23_000.0   # higher delta = ITM
    assert result.computed_delta == 0.66


# ── Delta tolerance rejection ─────────────────────────────────────────────────

def test_rejects_outside_delta_tolerance(session, monkeypatch):
    # All deltas far from 0.65 → NoValidStrikeError.
    insts = [_make_inst(23_500.0, 3001), _make_inst(24_000.0, 3002)]
    for i in insts:
        session.add(i)
    session.commit()

    far_deltas = {23_500.0: 0.30, 24_000.0: 0.20}

    def _far(instrument, ltp, underlying, t):
        return GreeksResult(delta=far_deltas[instrument.strike], iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = _make_quotes(insts)
    monkeypatch.setattr("app.strike_selector.compute_delta", _far)

    with pytest.raises(NoValidStrikeError, match="exceeds tolerance"):
        select_strike(
            "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
            segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
            now=_TODAY_NOW, settings=_s(),
        )


# ── Liquidity guardrails ──────────────────────────────────────────────────────

def test_rejects_low_premium(session, monkeypatch):
    inst = _make_inst(24_750.0, 4001)
    session.add(inst)
    session.commit()

    def _ok(instrument, ltp, underlying, t):
        return GreeksResult(delta=0.65, iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        f"NFO:{inst.tradingsymbol}": {
            "last_price": 3.0,       # below MIN_OPTION_PREMIUM_INDEX=5
            "oi": 5_000,
            "depth": {"buy": [{"price": 2.5}], "sell": [{"price": 3.5}]},
        }
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _ok)

    with pytest.raises(NoValidStrikeError):
        select_strike(
            "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
            segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
            now=_TODAY_NOW, settings=_s(MIN_OPTION_PREMIUM_INDEX=5.0),
        )


def test_rejects_low_oi(session, monkeypatch):
    inst = _make_inst(23_250.0, 5001)
    session.add(inst)
    session.commit()

    def _ok(instrument, ltp, underlying, t):
        return GreeksResult(delta=0.65, iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        f"NFO:{inst.tradingsymbol}": {
            "last_price": 100.0,
            "oi": 50,                # below MIN_OI_INDEX=1000
            "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100.5}]},
        }
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _ok)

    with pytest.raises(NoValidStrikeError):
        select_strike(
            "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
            segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
            now=_TODAY_NOW, settings=_s(MIN_OI_INDEX=1_000),
        )


def test_rejects_wide_spread(session, monkeypatch):
    inst = _make_inst(23_250.0, 6001)
    session.add(inst)
    session.commit()

    def _ok(instrument, ltp, underlying, t):
        return GreeksResult(delta=0.65, iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        f"NFO:{inst.tradingsymbol}": {
            "last_price": 100.0,
            "oi": 5_000,
            "depth": {
                "buy":  [{"price": 90.0}],   # spread = 10/100 = 10% > MAX_SPREAD_PCT=5%
                "sell": [{"price": 100.0}],
            },
        }
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _ok)

    with pytest.raises(NoValidStrikeError):
        select_strike(
            "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
            segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
            now=_TODAY_NOW, settings=_s(MAX_SPREAD_PCT=0.05),
        )


def test_skips_zero_price_strike(session, monkeypatch):
    # Two strikes: one with LTP=0 (skipped), one valid.
    inst_zero = _make_inst(24_500.0, 7001)
    inst_ok   = _make_inst(23_250.0, 7002)
    for i in [inst_zero, inst_ok]:
        session.add(i)
    session.commit()

    def _ok(instrument, ltp, underlying, t):
        return GreeksResult(delta=0.65, iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        f"NFO:{inst_zero.tradingsymbol}": {"last_price": 0.0, "oi": 5_000, "depth": {}},
        f"NFO:{inst_ok.tradingsymbol}": {
            "last_price": 100.0, "oi": 5_000,
            "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100.5}]},
        },
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _ok)

    result = select_strike(
        "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
        segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
        now=_TODAY_NOW, settings=_s(),
    )
    assert result.instrument.strike == 23_250.0
    assert inst_zero.tradingsymbol in result.rejection_reasons
    assert result.rejection_reasons[inst_zero.tradingsymbol] == "zero_price"


def test_skips_greeks_rejection(session, monkeypatch):
    # Strike where compute_delta returns a rejection → should be skipped.
    inst_bad = _make_inst(24_500.0, 8001)
    inst_ok  = _make_inst(23_250.0, 8002)
    for i in [inst_bad, inst_ok]:
        session.add(i)
    session.commit()

    def _mixed(instrument, ltp, underlying, t):
        if instrument.strike == 24_500.0:
            return GreeksResult(None, None, None, "near_expiry")
        return GreeksResult(delta=0.65, iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        f"NFO:{inst_bad.tradingsymbol}": {
            "last_price": 5.0, "oi": 5_000,
            "depth": {"buy": [{"price": 4.5}], "sell": [{"price": 5.5}]},
        },
        f"NFO:{inst_ok.tradingsymbol}": {
            "last_price": 100.0, "oi": 5_000,
            "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100.5}]},
        },
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _mixed)

    result = select_strike(
        "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
        segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
        now=_TODAY_NOW, settings=_s(),
    )
    assert result.instrument.strike == 23_250.0
    assert result.rejection_reasons.get(inst_bad.tradingsymbol) == "near_expiry"


# ── No strikes in DB ──────────────────────────────────────────────────────────

def test_no_strikes_raises(session, monkeypatch):
    kite = MagicMock()
    with pytest.raises(NoValidStrikeError, match="No CE instruments"):
        select_strike(
            "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
            segment="NFO-OPT", now=_TODAY_NOW, settings=_s(),
        )


# ── Audit log ─────────────────────────────────────────────────────────────────

def test_persists_strike_decision_audit_row(session, monkeypatch):
    insts = _populate_chain(session)
    kite = MagicMock()
    kite.quote.return_value = _make_quotes(insts)
    monkeypatch.setattr("app.strike_selector.compute_delta", _mock_compute_delta)

    select_strike(
        "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
        segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
        now=_TODAY_NOW, settings=_s(),
    )

    rows = session.query(StrikeDecision).all()
    assert len(rows) == 1
    sd = rows[0]
    assert sd.underlying == "NIFTY"
    assert sd.expiry == str(_EXPIRY)
    assert sd.flag == "c"
    assert sd.target_delta == 0.65
    assert sd.selected_tradingsymbol is not None
    assert sd.selected_delta is not None
    # All 10 candidates serialised in JSON
    candidates = json.loads(sd.candidates_json)
    assert len(candidates) == len(_STRIKES)


def test_audit_row_on_rejection(session, monkeypatch):
    # When selection fails, audit row still written with rejection_reason=None
    # (the reason is recorded in the exception message, not the DB field here — the DB
    # field is for strike-level rejection; NoValidStrikeError is the signal to caller).
    inst = _make_inst(24_750.0, 9001)
    session.add(inst)
    session.commit()

    far_deltas = {24_750.0: 0.10}

    def _far(instrument, ltp, underlying, t):
        return GreeksResult(delta=far_deltas.get(instrument.strike, 0.10), iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        "NFO:NIFTY25116247500CE": {
            "last_price": 10.0, "oi": 5_000,
            "depth": {"buy": [{"price": 9.5}], "sell": [{"price": 10.5}]},
        }
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _far)

    with pytest.raises(NoValidStrikeError):
        select_strike(
            "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
            segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
            now=_TODAY_NOW, settings=_s(),
        )

    rows = session.query(StrikeDecision).all()
    assert len(rows) == 1
    assert rows[0].selected_tradingsymbol is None


# ── MCX / Black-76 routing ────────────────────────────────────────────────────

def test_mcx_dispatches_black76(session, monkeypatch):
    # MCX-OPT segment; verify compute_delta is called with an MCX-segment instrument.
    inst = _make_inst(5_000.0, 10001, itype="CE", segment="MCX-OPT")
    inst.exchange = "MCX"
    inst.tradingsymbol = "CRUDEOIL25JAN5000CE"
    inst.name = "CRUDEOIL"
    session.add(inst)
    session.commit()

    captured = {}

    def _capture(instrument, ltp, underlying, t):
        captured["segment"] = instrument.segment
        return GreeksResult(delta=0.65, iv=0.30, model_used="Black-76", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        "MCX:CRUDEOIL25JAN5000CE": {
            "last_price": 50.0, "oi": 200,
            "depth": {"buy": [{"price": 49.0}], "sell": [{"price": 51.0}]},
        }
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _capture)

    result = select_strike(
        "CRUDEOIL", _EXPIRY, "CE", kite, 5_000.0, session,
        segment="MCX-OPT", target_delta=0.65, tolerance=0.05,
        now=_TODAY_NOW,
        settings=_s(MIN_OPTION_PREMIUM_STOCK=2.0, MIN_OI_STOCK=100, WEEKLY_INDICES=[]),
    )

    assert "MCX" in captured["segment"]
    assert result.computed_delta == 0.65


# ── Missing depth — spread check skipped ─────────────────────────────────────

def test_missing_depth_skips_spread_check(session, monkeypatch):
    inst = _make_inst(23_250.0, 11001)
    session.add(inst)
    session.commit()

    def _ok(instrument, ltp, underlying, t):
        return GreeksResult(delta=0.65, iv=_IV, model_used="BSM", rejection_reason=None)

    kite = MagicMock()
    kite.quote.return_value = {
        f"NFO:{inst.tradingsymbol}": {
            "last_price": 100.0,
            "oi": 5_000,
            # no "depth" key at all
        }
    }
    monkeypatch.setattr("app.strike_selector.compute_delta", _ok)

    # Should NOT raise; spread check skipped when depth missing.
    result = select_strike(
        "NIFTY", _EXPIRY, "CE", kite, _SPOT, session,
        segment="NFO-OPT", target_delta=0.65, tolerance=0.05,
        now=_TODAY_NOW, settings=_s(),
    )
    assert result.instrument.strike == 23_250.0
