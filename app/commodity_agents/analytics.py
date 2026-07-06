"""Deterministic vol analytics — the numbers a premium seller checks first:

  * VRP (volatility risk premium): ATM IV minus realized vol. Positive means
    the market pays you more than the underlying actually moves.
  * IV trend: expanding / contracting / stable, from the stored run history.
    VRP says whether premium is rich; the trend says which way it is heading.
  * Expected move vs breakevens: the ATM straddle price IS the market-implied
    move; compare it to the realized pace and to the straddle's breakevens.
  * Scenario stress: P&L of 1 lot short ATM straddle at ±1/2/3% underlying
    moves — held to expiry, and under an instant +5-vol-point IV shock.

Pure math, no LLM, no I/O. Everything degrades gracefully to partial output
when an input is missing (e.g. no realized vol yet).
"""
from __future__ import annotations

import math
from datetime import date, datetime

from py_vollib.black import black as _b76_price
from py_vollib.black_scholes_merton import black_scholes_merton as _bsm_price

from app.commodity_agents.models import StrikeCandidate, exchange_for
from app.config import IST

_SECONDS_PER_YEAR = 31_557_600
_MIN_T = 1.0 / 8760                     # 1 hour, matches greeks.py
_EXPIRY_HHMM = {"MCX": (17, 0), "NFO": (15, 30)}
_STRESS_MOVES = (-0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03)
_IV_SHOCK = 0.05                        # +5 vol points instant-shock column
_TREND_THRESHOLD = 0.05                 # ±5% IV change ⇒ expanding/contracting


def _price(exch: str, flag: str, under: float, strike: float,
           t: float, r: float, sigma: float) -> float | None:
    try:
        if exch == "NFO":
            p = _bsm_price(flag, under, strike, t, r, sigma, 0.0)
        else:
            p = _b76_price(flag, under, strike, t, r, sigma)
        return None if math.isnan(p) else p
    except Exception:
        return None


def iv_trend(iv_points: list[float]) -> dict:
    """Direction of ATM IV across pipeline runs (oldest → newest, current
    sample included as the last point): mean of the last 3 samples vs the mean
    of up to 10 samples before them."""
    n = len(iv_points)
    if n < 6:
        return {"direction": "insufficient-history", "change_pct": None, "samples": n}
    recent = iv_points[-3:]
    prior = iv_points[max(0, n - 13):n - 3]
    prior_mean = sum(prior) / len(prior)
    if prior_mean <= 0:
        return {"direction": "insufficient-history", "change_pct": None, "samples": n}
    chg = (sum(recent) / len(recent) - prior_mean) / prior_mean
    if chg >= _TREND_THRESHOLD:
        direction = "expanding"
    elif chg <= -_TREND_THRESHOLD:
        direction = "contracting"
    else:
        direction = "stable"
    return {"direction": direction, "change_pct": round(chg * 100.0, 1), "samples": n}


def _atm_pair(candidates: list[StrikeCandidate]) -> tuple[StrikeCandidate | None, StrikeCandidate | None]:
    atm = [c for c in candidates if c.target_delta == 0.50]
    ce = next((c for c in atm if c.instrument_type == "CE"), None)
    pe = next((c for c in atm if c.instrument_type == "PE"), None)
    return ce, pe


def _time_to_expiry_years(expiry_iso: str, exch: str, now: datetime) -> float | None:
    try:
        exp = date.fromisoformat(expiry_iso)
    except (TypeError, ValueError):
        return None
    expiry_dt = datetime(exp.year, exp.month, exp.day, *_EXPIRY_HHMM[exch], tzinfo=IST)
    return max((expiry_dt - now).total_seconds() / _SECONDS_PER_YEAR, _MIN_T)


def skew_25d(candidates: list[StrikeCandidate]) -> dict | None:
    """25-delta risk reversal: put IV minus call IV, in vol points.
    Positive (puts richer) = the market is paying up for crash protection."""
    wing = [c for c in candidates if c.target_delta == 0.25 and c.iv is not None]
    ce = next((c for c in wing if c.instrument_type == "CE"), None)
    pe = next((c for c in wing if c.instrument_type == "PE"), None)
    if ce is None or pe is None:
        return None
    rr = (pe.iv - ce.iv) * 100.0
    if rr > 1.0:
        read = "put skew steep — downside fear priced in; short put side carries extra risk"
    elif rr < -1.0:
        read = "call skew — upside chase priced in; short call side carries extra risk"
    else:
        read = "skew flat — no directional fear premium"
    return {"rr_25d_pts": round(rr, 2), "put_iv_pct": round(pe.iv * 100.0, 2),
            "call_iv_pct": round(ce.iv * 100.0, 2), "read": read}


def term_structure(near_iv: float | None, next_iv: float | None) -> dict | None:
    """Front-expiry ATM IV vs next-expiry ATM IV."""
    if not near_iv or not next_iv:
        return None
    ratio = near_iv / next_iv
    if ratio >= 1.05:
        read = ("inverted (front rich) — event premium loaded in the near expiry; "
                "harvestable if the event passes quietly")
    elif ratio <= 0.95:
        read = "contango (front cheap) — no near-term event premium to harvest"
    else:
        read = "flat term structure"
    return {"near_iv_pct": round(near_iv * 100.0, 2),
            "next_iv_pct": round(next_iv * 100.0, 2),
            "ratio": round(ratio, 3), "read": read}


def compute_analytics(
    commodity: str,
    candidates: list[StrikeCandidate],
    atm_iv: float | None,
    realized_vol: float | None,
    future_price: float | None,
    now: datetime,
    iv_history_asc: list[float],
    risk_free_rate: float = 0.07,
    next_atm_iv: float | None = None,
    positioning: dict | None = None,
    india_vix: float | None = None,
) -> dict:
    """iv_history_asc is oldest→newest and must already include the current
    run's ATM IV as its last sample (the orchestrator commits atm_iv before
    calling this, so the plain history query satisfies that)."""
    out: dict = {}

    skew = skew_25d(candidates)
    if skew:
        out["skew"] = skew
    ts = term_structure(atm_iv, next_atm_iv)
    if ts:
        out["term_structure"] = ts
    if positioning:
        out["positioning"] = positioning
    if india_vix is not None:
        out["india_vix"] = india_vix

    if atm_iv is not None and realized_vol:
        vrp = atm_iv - realized_vol
        out["vrp"] = {
            "atm_iv_pct": round(atm_iv * 100.0, 2),
            "realized_vol_pct": round(realized_vol * 100.0, 2),
            "vrp_pts": round(vrp * 100.0, 2),
            "read": (
                "IV above realized — the seller is being paid a premium over actual movement"
                if vrp > 0 else
                "IV below realized — premium is cheap, selling has no cushion"
            ),
        }

    out["iv_trend"] = iv_trend(iv_history_asc)

    ce, pe = _atm_pair(candidates)
    if ce is None or pe is None or not future_price:
        return out
    credit = ce.ltp + pe.ltp
    if credit <= 0:
        return out
    exch = exchange_for(commodity)
    t_years = _time_to_expiry_years(ce.expiry, exch, now)
    if t_years is None:
        return out

    realized_pts = (
        realized_vol * math.sqrt(t_years) * future_price if realized_vol else None
    )
    out["expected_move"] = {
        "underlying_price": round(future_price, 2),   # calibration reference
        "implied_move_pts": round(credit, 2),
        "implied_move_pct": round(credit / future_price * 100.0, 2),
        "realized_move_pts": round(realized_pts, 2) if realized_pts else None,
        # >1: options price more movement than the underlying delivers (seller edge)
        "edge_ratio": round(credit / realized_pts, 2) if realized_pts else None,
        "breakeven_upper": round(ce.strike + credit, 2),
        "breakeven_lower": round(pe.strike - credit, 2),
        "days_to_expiry": round(t_years * 365.25, 1),
    }

    iv_ce = ce.iv or atm_iv
    iv_pe = pe.iv or atm_iv
    rows = []
    for m in _STRESS_MOVES:
        moved = future_price * (1.0 + m)
        intrinsic = max(moved - ce.strike, 0.0) + max(pe.strike - moved, 0.0)
        pnl_expiry = (credit - intrinsic) * ce.lot_units
        pnl_shock = None
        if iv_ce and iv_pe:
            c2 = _price(exch, "c", moved, ce.strike, t_years, risk_free_rate, iv_ce + _IV_SHOCK)
            p2 = _price(exch, "p", moved, pe.strike, t_years, risk_free_rate, iv_pe + _IV_SHOCK)
            if c2 is not None and p2 is not None:
                pnl_shock = (credit - c2 - p2) * ce.lot_units
        rows.append({
            "move_pct": round(m * 100.0, 1),
            "pnl_at_expiry": round(pnl_expiry),
            "pnl_iv_up5": round(pnl_shock) if pnl_shock is not None else None,
        })
    out["stress"] = {
        "basis": "1 lot short ATM straddle, INR",
        "lot_units": ce.lot_units,
        "iv_shock_pts": round(_IV_SHOCK * 100),
        "rows": rows,
    }
    return out
