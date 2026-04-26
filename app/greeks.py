from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.optimize import brentq

from app.config import get_settings
from app.storage import Instrument

from py_vollib.black_scholes_merton.implied_volatility import implied_volatility as _bsm_iv_fn
from py_vollib.black_scholes_merton.greeks.analytical import delta as _bsm_delta_fn
from py_vollib.black_scholes_merton import black_scholes_merton as _bsm_price_fn
from py_vollib.black.implied_volatility import implied_volatility as _b76_iv_fn
from py_vollib.black.greeks.analytical import delta as _b76_delta_fn
from py_vollib.black import black as _b76_price_fn

# py_vollib confirmed signatures (from inspect):
#   _bsm_iv_fn   (price, S, K, t, r, q, flag)
#   _bsm_delta_fn(flag, S, K, t, r, sigma, q)   ← q is LAST
#   _bsm_price_fn(flag, S, K, t, r, sigma, q)
#   _b76_iv_fn   (price, F, K, r, t, flag)       ← r before t
#   _b76_delta_fn(flag, F, K, t, r, sigma)        ← t before r
#   _b76_price_fn(flag, F, K, t, r, sigma)

_MIN_T = 1.0 / 8760        # 1 hour expressed in years
_IV_LO = 0.001
_IV_HI = 5.0

_MCX_SEGMENTS = frozenset({"MCX", "MCX-OPT"})


@dataclass(frozen=True)
class GreeksResult:
    delta: float | None
    iv: float | None
    model_used: str | None
    rejection_reason: str | None  # None means success


# ── Low-level IV solvers ──────────────────────────────────────────────────────

def implied_volatility_bsm(
    option_price: float,
    S: float,
    K: float,
    t: float,
    r: float,
    q: float,
    flag: str,
) -> float | None:
    """Back-solve implied volatility for an NSE option via Black-Scholes-Merton.

    Returns the converged IV (which may be outside [IV_LO, IV_HI] if py_vollib's
    analytical solver runs away) or None when all solvers fail to converge.
    Callers should range-check the returned value.
    """
    try:
        iv = _bsm_iv_fn(option_price, S, K, t, r, q, flag)
        if not math.isnan(iv):
            return iv
    except Exception:
        pass
    # brentq fallback constrained to [IV_LO, IV_HI]
    try:
        def fn(sigma: float) -> float:
            return _bsm_price_fn(flag, S, K, t, r, sigma, q) - option_price
        lo_err = fn(_IV_LO)
        hi_err = fn(_IV_HI)
        if lo_err * hi_err >= 0:
            return None
        return brentq(fn, _IV_LO, _IV_HI, xtol=1e-6, maxiter=100)
    except Exception:
        return None


def delta_bsm(
    S: float,
    K: float,
    t: float,
    r: float,
    q: float,
    sigma: float,
    flag: str,
) -> float | None:
    """BSM analytical delta. Returns None on any failure or NaN result."""
    try:
        d = _bsm_delta_fn(flag, S, K, t, r, sigma, q)
        return None if math.isnan(d) else d
    except Exception:
        return None


def implied_volatility_b76(
    option_price: float,
    F: float,
    K: float,
    r: float,
    t: float,
    flag: str,
) -> float | None:
    """Back-solve implied volatility for an MCX option via Black-76.

    Despite py_vollib's parameter name 'discounted_option_price', this function
    accepts the raw (undiscounted) market price — confirmed by round-trip test.
    """
    try:
        iv = _b76_iv_fn(option_price, F, K, r, t, flag)
        if not math.isnan(iv):
            return iv
    except Exception:
        pass
    # brentq fallback constrained to [IV_LO, IV_HI]
    try:
        def fn(sigma: float) -> float:
            return _b76_price_fn(flag, F, K, t, r, sigma) - option_price
        lo_err = fn(_IV_LO)
        hi_err = fn(_IV_HI)
        if lo_err * hi_err >= 0:
            return None
        return brentq(fn, _IV_LO, _IV_HI, xtol=1e-6, maxiter=100)
    except Exception:
        return None


def delta_b76(
    F: float,
    K: float,
    t: float,
    r: float,
    sigma: float,
    flag: str,
) -> float | None:
    """Black-76 analytical delta. Returns None on any failure or NaN result."""
    try:
        d = _b76_delta_fn(flag, F, K, t, r, sigma)
        return None if math.isnan(d) else d
    except Exception:
        return None


# ── Dispatcher ────────────────────────────────────────────────────────────────

def compute_delta(
    instrument: Instrument,
    option_ltp: float,
    underlying_price: float,
    time_to_expiry_years: float,
) -> GreeksResult:
    """Compute delta for an option instrument. Never raises — errors become rejection_reason.

    instrument:            Kite Instrument row (segment drives model selection)
    option_ltp:            latest traded price of the option
    underlying_price:      spot price for NSE/BFO instruments; futures price for MCX
    time_to_expiry_years:  (expiry_datetime - now).total_seconds() / 31_557_600
    """
    if time_to_expiry_years < _MIN_T:
        return GreeksResult(None, None, None, "near_expiry")
    if option_ltp <= 0:
        return GreeksResult(None, None, None, "zero_price")

    flag = 'c' if instrument.instrument_type == "CE" else 'p'
    K = float(instrument.strike or 0.0)
    settings = get_settings()
    r = settings.RISK_FREE_RATE
    seg = instrument.segment.upper()
    is_mcx = seg in _MCX_SEGMENTS

    if is_mcx:
        model = "Black-76"
        iv = implied_volatility_b76(option_ltp, underlying_price, K, r, time_to_expiry_years, flag)
        if iv is None:
            return GreeksResult(None, None, model, "iv_solver_failed")
        if not (_IV_LO <= iv <= _IV_HI):
            return GreeksResult(None, iv, model, "iv_out_of_range")
        d = delta_b76(underlying_price, K, time_to_expiry_years, r, iv, flag)
    else:
        model = "BSM"
        q = settings.DIVIDEND_YIELD_OVERRIDES.get(
            instrument.name, settings.DIVIDEND_YIELD_DEFAULT
        )
        iv = implied_volatility_bsm(
            option_ltp, underlying_price, K, time_to_expiry_years, r, q, flag
        )
        if iv is None:
            return GreeksResult(None, None, model, "iv_solver_failed")
        if not (_IV_LO <= iv <= _IV_HI):
            return GreeksResult(None, iv, model, "iv_out_of_range")
        d = delta_bsm(underlying_price, K, time_to_expiry_years, r, q, iv, flag)

    if d is None:
        return GreeksResult(None, iv, model, "delta_out_of_range")

    # Sanity-check delta bounds (calls: 0–1, puts: -1–0) with float tolerance.
    tol = 1e-9
    if flag == 'c' and not (-tol <= d <= 1.0 + tol):
        return GreeksResult(None, iv, model, "delta_out_of_range")
    if flag == 'p' and not (-1.0 - tol <= d <= tol):
        return GreeksResult(None, iv, model, "delta_out_of_range")

    return GreeksResult(delta=d, iv=iv, model_used=model, rejection_reason=None)
