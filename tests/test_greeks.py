"""Tests for app/greeks.py — all offline, no network, no Kite connection."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.greeks import (
    GreeksResult,
    compute_delta,
    delta_b76,
    delta_bsm,
    implied_volatility_b76,
    implied_volatility_bsm,
)
from app.storage import Instrument
from py_vollib.black import black as b76_price
from py_vollib.black_scholes_merton import black_scholes_merton as bsm_price


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_inst(
    name: str,
    strike: float,
    instrument_type: str,   # "CE" or "PE"
    segment: str,           # "NFO", "BFO", "MCX", etc.
    exchange: str,
    token: int = 1,
) -> Instrument:
    inst = Instrument()
    inst.instrument_token = token
    inst.exchange_token = token
    inst.tradingsymbol = f"{name}{instrument_type}{int(strike)}"
    inst.name = name
    inst.expiry = None
    inst.strike = strike
    inst.tick_size = 0.05
    inst.lot_size = 50
    inst.instrument_type = instrument_type
    inst.segment = segment
    inst.exchange = exchange
    return inst


def _s(**kwargs) -> Settings:
    """Build a Settings object without reading any .env file."""
    return Settings(_env_file=None, **kwargs)


# ── Reference parameters ──────────────────────────────────────────────────────
# From v2 spec §9: S=25000 K=25000 t=7/365 r=0.065 sigma=0.15 q=0
_S, _K, _T, _R, _Q, _SIG = 25_000.0, 25_000.0, 7 / 365, 0.065, 0.0, 0.15

# Black-76 reference: F=85 K=85 t=30/365 r=0.065 sigma=0.30
_F76, _K76, _T76, _R76, _SIG76 = 85.0, 85.0, 30 / 365, 0.065, 0.30


# ── BSM delta ─────────────────────────────────────────────────────────────────

def test_bsm_call_delta():
    d = delta_bsm(_S, _K, _T, _R, _Q, _SIG, 'c')
    assert d is not None
    assert abs(d - 0.52) < 0.02, f"expected ~0.52, got {d:.4f}"


def test_bsm_put_delta():
    d = delta_bsm(_S, _K, _T, _R, _Q, _SIG, 'p')
    assert d is not None
    assert abs(d - (-0.48)) < 0.02, f"expected ~-0.48, got {d:.4f}"


def test_bsm_itm_call_delta():
    # Strike 24000 (ITM) — delta should be well above ATM (~0.52)
    d = delta_bsm(_S, 24_000.0, _T, _R, _Q, _SIG, 'c')
    assert d is not None
    assert d > 0.55, f"ITM call delta should be > 0.55, got {d:.4f}"


def test_bsm_otm_call_delta():
    # Strike 26000 (OTM) — delta should be well below ATM (~0.52)
    d = delta_bsm(_S, 26_000.0, _T, _R, _Q, _SIG, 'c')
    assert d is not None
    assert d < 0.45, f"OTM call delta should be < 0.45, got {d:.4f}"


# ── BSM IV round-trip ─────────────────────────────────────────────────────────

def test_bsm_iv_round_trip():
    known_sigma = 0.20
    ref_price = bsm_price('c', _S, _K, _T, _R, known_sigma, _Q)
    iv = implied_volatility_bsm(ref_price, _S, _K, _T, _R, _Q, 'c')
    assert iv is not None
    assert abs(iv - known_sigma) < 0.005, f"IV round-trip: expected {known_sigma}, got {iv:.6f}"


# ── Black-76 delta ────────────────────────────────────────────────────────────

def test_b76_call_delta():
    d = delta_b76(_F76, _K76, _T76, _R76, _SIG76, 'c')
    assert d is not None
    assert abs(d - 0.52) < 0.02, f"expected ~0.52, got {d:.4f}"


def test_b76_put_delta():
    d = delta_b76(_F76, _K76, _T76, _R76, _SIG76, 'p')
    assert d is not None
    assert abs(d - (-0.48)) < 0.02, f"expected ~-0.48, got {d:.4f}"


# ── Black-76 IV round-trip ────────────────────────────────────────────────────

def test_b76_iv_round_trip():
    known_sigma = 0.30
    ref_price = b76_price('c', _F76, _K76, _T76, _R76, known_sigma)
    iv = implied_volatility_b76(ref_price, _F76, _K76, _R76, _T76, 'c')
    assert iv is not None
    assert abs(iv - known_sigma) < 0.005, f"IV round-trip: expected {known_sigma}, got {iv:.6f}"


# ── Dispatcher routing ────────────────────────────────────────────────────────

def test_dispatcher_routes_nfo_to_bsm(monkeypatch):
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    inst = _make_inst("NIFTY", _K, "CE", "NFO", "NFO")
    ref_price = bsm_price('c', _S, _K, _T, _R, _SIG, _Q)
    result = compute_delta(inst, ref_price, _S, _T)
    assert result.rejection_reason is None
    assert result.model_used == "BSM"


def test_dispatcher_routes_bfo_to_bsm(monkeypatch):
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    inst = _make_inst("SENSEX", _K, "CE", "BFO", "BFO")
    ref_price = bsm_price('c', _S, _K, _T, _R, _SIG, _Q)
    result = compute_delta(inst, ref_price, _S, _T)
    assert result.rejection_reason is None
    assert result.model_used == "BSM"


def test_dispatcher_routes_mcx_to_b76(monkeypatch):
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    inst = _make_inst("CRUDEOIL", _K76, "CE", "MCX", "MCX")
    ref_price = b76_price('c', _F76, _K76, _T76, _R76, _SIG76)
    result = compute_delta(inst, ref_price, _F76, _T76)
    assert result.rejection_reason is None
    assert result.model_used == "Black-76"


# ── Rejection guards ──────────────────────────────────────────────────────────

def test_rejection_near_expiry(monkeypatch):
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    inst = _make_inst("NIFTY", _K, "CE", "NFO", "NFO")
    result = compute_delta(inst, 100.0, _S, 1.0 / 8760 - 1e-9)
    assert result.rejection_reason == "near_expiry"
    assert result.delta is None


def test_rejection_zero_price(monkeypatch):
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    inst = _make_inst("NIFTY", _K, "CE", "NFO", "NFO")
    result = compute_delta(inst, 0.0, _S, _T)
    assert result.rejection_reason == "zero_price"


def test_rejection_negative_price(monkeypatch):
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    inst = _make_inst("NIFTY", _K, "CE", "NFO", "NFO")
    result = compute_delta(inst, -1.0, _S, _T)
    assert result.rejection_reason == "zero_price"


def test_rejection_iv_solver_failed(monkeypatch):
    # ITM put (K=26000, S=25000) with option_price=0.05 — way below intrinsic (~1000).
    # No sigma in [0.001, 5.0] produces such a low price, so both solvers should fail.
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    inst = _make_inst("NIFTY", 26_000.0, "PE", "NFO", "NFO")
    result = compute_delta(inst, 0.05, _S, _T)
    assert result.rejection_reason in ("iv_solver_failed", "iv_out_of_range")
    assert result.delta is None


# ── Dividend-yield override ───────────────────────────────────────────────────

def test_dispatcher_q_override(monkeypatch):
    # Generate a price using q=0.025 at sigma=0.15.
    # When the dispatcher is given an INFY instrument with override q=0.025,
    # the IV back-solver should recover sigma≈0.15 (round-trip).
    # With the wrong q=0.0 the back-solved IV would differ from 0.15.
    custom = _s(DIVIDEND_YIELD_OVERRIDES={"INFY": 0.025})
    monkeypatch.setattr("app.greeks.get_settings", lambda: custom)

    ref_price = bsm_price('c', _S, _K, _T, _R, _SIG, 0.025)
    inst = _make_inst("INFY", _K, "CE", "NFO", "NFO")
    result = compute_delta(inst, ref_price, _S, _T)

    assert result.rejection_reason is None, f"unexpected rejection: {result.rejection_reason}"
    assert result.model_used == "BSM"
    # IV should round-trip to ~0.15 because correct q=0.025 was used
    assert result.iv is not None
    assert abs(result.iv - _SIG) < 0.005, (
        f"IV round-trip failed: expected {_SIG}, got {result.iv:.6f} — "
        "suggests wrong q was used in back-solver"
    )


def test_dispatcher_default_q(monkeypatch):
    # NIFTY has no override; DIVIDEND_YIELD_DEFAULT=0.0 is used.
    monkeypatch.setattr("app.greeks.get_settings", lambda: _s())
    ref_price = bsm_price('c', _S, _K, _T, _R, _SIG, 0.0)
    inst = _make_inst("NIFTY", _K, "CE", "NFO", "NFO")
    result = compute_delta(inst, ref_price, _S, _T)
    assert result.rejection_reason is None
    assert result.iv is not None
    assert abs(result.iv - _SIG) < 0.005
