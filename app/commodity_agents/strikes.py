"""Delta-targeted strike candidate engine — deterministic, reuses Black-76.

Gives the debate agents *concrete* option candidates (symbol, strike, IV,
delta, premium) at the 0.50 / 0.65 / 0.80 delta buckets, so they argue about
real tradeable strikes rather than abstract ideas.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable

from app.commodity_agents.models import StrikeCandidate, exchange_for
from app.config import IST, get_settings
from app.greeks import compute_delta
from app.storage import Instrument

log = logging.getLogger(__name__)

TARGET_DELTAS = (0.25, 0.50, 0.65, 0.80)   # 0.25 = wings (iron fly) + skew read
_ATM_BAND_PCT = 0.10          # only quote strikes within ±10% of the underlying
_SECONDS_PER_YEAR = 31_557_600
# Expiry cutoff time by exchange; being off by hours is immaterial at daily scale
_EXPIRY_HHMM = {"MCX": (17, 0), "NFO": (15, 30)}


def nearest_option_expiry(session: Any, underlying: str, today: date) -> date | None:
    row = (
        session.query(Instrument.expiry)
        .filter(
            Instrument.name == underlying,
            Instrument.exchange == exchange_for(underlying),
            Instrument.instrument_type.in_(["CE", "PE"]),
            Instrument.expiry >= today,
        )
        .order_by(Instrument.expiry.asc())
        .first()
    )
    return row[0] if row else None


def next_option_expiry(session: Any, underlying: str, after: date) -> date | None:
    """First option expiry strictly after `after` — the term-structure back leg."""
    row = (
        session.query(Instrument.expiry)
        .filter(
            Instrument.name == underlying,
            Instrument.exchange == exchange_for(underlying),
            Instrument.instrument_type.in_(["CE", "PE"]),
            Instrument.expiry > after,
        )
        .order_by(Instrument.expiry.asc())
        .first()
    )
    return row[0] if row else None


def front_month_future(session: Any, underlying: str, today: date) -> Instrument | None:
    return (
        session.query(Instrument)
        .filter(
            Instrument.name == underlying,
            Instrument.instrument_type == "FUT",
            Instrument.exchange == exchange_for(underlying),
            Instrument.expiry >= today,
        )
        .order_by(Instrument.expiry.asc())
        .first()
    )


def build_strike_candidates(
    session: Any,
    quote_fn: Callable[[list[str]], dict],
    underlying: str,
    future_price: float,
    now: datetime | None = None,
    target_deltas: tuple[float, ...] = TARGET_DELTAS,
    expiry: date | None = None,
) -> list[StrikeCandidate]:
    return build_chain_snapshot(session, quote_fn, underlying, future_price,
                                now=now, target_deltas=target_deltas,
                                expiry=expiry)[0]


def build_chain_snapshot(
    session: Any,
    quote_fn: Callable[[list[str]], dict],
    underlying: str,
    future_price: float,
    now: datetime | None = None,
    target_deltas: tuple[float, ...] = TARGET_DELTAS,
    expiry: date | None = None,
) -> tuple[list[StrikeCandidate], dict]:
    """For each target delta and each side (CE/PE), pick the strike whose
    computed |delta| lands closest to the target. Also returns a positioning
    dict (PCR, max pain, OI walls) computed from the same quote round — open
    interest tells you where writers are defending, at zero extra API cost.

    future_price is the underlying reference price: front future for MCX,
    index spot for NFO (compute_delta dispatches Black-76 vs BSM by segment).
    quote_fn takes ["MCX:SYMBOL", ...] and returns kite.quote()-shaped dicts —
    injected so tests can fake it and the orchestrator can pass kite.quote.
    """
    now = now or datetime.now(IST)
    settings = get_settings()
    exch = exchange_for(underlying)
    if expiry is None:
        expiry = nearest_option_expiry(session, underlying, now.date())
    if expiry is None:
        log.warning("[strikes] no option expiry found for %s", underlying)
        return [], {}

    lo = future_price * (1 - _ATM_BAND_PCT)
    hi = future_price * (1 + _ATM_BAND_PCT)
    rows: list[Instrument] = (
        session.query(Instrument)
        .filter(
            Instrument.name == underlying,
            Instrument.exchange == exch,
            Instrument.instrument_type.in_(["CE", "PE"]),
            Instrument.expiry == expiry,
            Instrument.strike >= lo,
            Instrument.strike <= hi,
        )
        .all()
    )
    if not rows:
        log.warning("[strikes] no strikes within ±10%% of %.2f for %s", future_price, underlying)
        return [], {}

    keys = [f"{exch}:{r.tradingsymbol}" for r in rows]
    try:
        quotes = quote_fn(keys)
    except Exception as exc:
        log.warning("[strikes] quote fetch failed for %s: %s", underlying, exc)
        return [], {}

    expiry_dt = datetime(expiry.year, expiry.month, expiry.day, *_EXPIRY_HHMM[exch], tzinfo=IST)
    t_years = max((expiry_dt - now).total_seconds() / _SECONDS_PER_YEAR, 0.0)
    # NFO rows carry the real lot size; MCX rows store lot_size=1 and the true
    # contract units live in MCX_LOT_UNITS.
    lot_units = settings.MCX_LOT_UNITS.get(underlying) or rows[0].lot_size

    # compute greeks for every quoted strike once; collect OI in the same pass
    evaluated: list[tuple[Instrument, float, Any]] = []
    oi_by_strike: dict[tuple[str, float], float] = {}
    for r in rows:
        q = quotes.get(f"{exch}:{r.tradingsymbol}")
        if not q:
            continue
        oi = float(q.get("oi") or 0.0)
        if oi > 0 and r.strike:
            key = (r.instrument_type, float(r.strike))
            oi_by_strike[key] = oi_by_strike.get(key, 0.0) + oi
        ltp = float(q.get("last_price") or 0.0)
        if ltp <= 0:
            continue
        g = compute_delta(r, ltp, future_price, t_years)
        evaluated.append((r, ltp, g))

    out: list[StrikeCandidate] = []
    for side in ("CE", "PE"):
        side_rows = [(r, ltp, g) for (r, ltp, g) in evaluated
                     if r.instrument_type == side and g.delta is not None]
        for td in target_deltas:
            if not side_rows:
                continue
            best = min(side_rows, key=lambda x: abs(abs(x[2].delta) - td))
            r, ltp, g = best
            out.append(StrikeCandidate(
                tradingsymbol=r.tradingsymbol,
                instrument_type=side,
                strike=float(r.strike or 0.0),
                expiry=expiry.isoformat(),
                ltp=ltp,
                iv=g.iv,
                delta=g.delta,
                target_delta=td,
                lot_units=lot_units,
            ))
    return out, _positioning(oi_by_strike)


def _positioning(oi_by_strike: dict[tuple[str, float], float]) -> dict:
    """PCR, max pain, and OI walls from the quoted band (±10% of underlying).

    Band-limited by construction — fine for the ATM story a premium seller
    cares about; far-OTM lottery-ticket OI is deliberately out of frame.
    """
    ce = {k: v for (t, k), v in oi_by_strike.items() if t == "CE"}
    pe = {k: v for (t, k), v in oi_by_strike.items() if t == "PE"}
    if not ce and not pe:
        return {}
    total_ce = sum(ce.values())
    total_pe = sum(pe.values())
    strikes_all = sorted(set(ce) | set(pe))
    # max pain: expiry price minimizing total intrinsic paid out to holders
    max_pain = None
    if strikes_all and (total_ce or total_pe):
        def payout(s: float) -> float:
            return (sum(oi * max(s - k, 0.0) for k, oi in ce.items())
                    + sum(oi * max(k - s, 0.0) for k, oi in pe.items()))
        max_pain = min(strikes_all, key=payout)
    return {
        "pcr": round(total_pe / total_ce, 2) if total_ce > 0 else None,
        "max_pain": max_pain,
        "call_wall": max(ce, key=ce.get) if ce else None,   # heaviest CE OI = overhead magnet/cap
        "put_wall": max(pe, key=pe.get) if pe else None,    # heaviest PE OI = support shelf
        "total_ce_oi": round(total_ce),
        "total_pe_oi": round(total_pe),
        "band_pct": _ATM_BAND_PCT * 100,
    }


def next_expiry_atm_iv(
    session: Any,
    quote_fn: Callable[[list[str]], dict],
    underlying: str,
    future_price: float,
    near_expiry: date,
    now: datetime | None = None,
) -> float | None:
    """ATM IV of the NEXT expiry — the back leg of the term structure."""
    nxt = next_option_expiry(session, underlying, near_expiry)
    if nxt is None:
        return None
    cands, _ = build_chain_snapshot(session, quote_fn, underlying, future_price,
                                    now=now, target_deltas=(0.50,), expiry=nxt)
    return atm_iv_from_candidates(cands)


def atm_iv_from_candidates(candidates: list[StrikeCandidate]) -> float | None:
    """Average CE/PE IV at the 0.50-delta bucket — the pipeline's 'ATM IV'."""
    ivs = [c.iv for c in candidates if c.target_delta == 0.50 and c.iv is not None]
    return sum(ivs) / len(ivs) if ivs else None
