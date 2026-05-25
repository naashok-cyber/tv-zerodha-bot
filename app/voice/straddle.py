"""Short straddle validation: ATM rounding, spread guard, margin check, delta sanity.

Called from two places:
  1. /voice/transcribe — pre-confirmation (shows report before user approves)
  2. _process_straddle  — execution-time re-validation (rejects if margin now insufficient)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import IST, Settings

log = logging.getLogger(__name__)

NG_TICK_SIZE: float = 0.05          # MCX NG option minimum tick
_SECONDS_PER_YEAR: float = 365.25 * 24 * 3600


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class StraddleLeg:
    tradingsymbol: str
    instrument_token: int
    strike: float
    ltp: float
    bid: float
    ask: float
    spread_abs: float
    spread_pct: float
    threshold_used: float   # actual ₹ max-spread threshold (after tick floor)
    valid: bool
    delta: float | None = None
    iv: float | None = None
    rejection_reason: str | None = None


@dataclass
class StraddleValidation:
    underlying: str
    atm_strike: float
    expiry: date
    quantity: int
    lot_size: int
    lot_units: int          # MCX_LOT_UNITS value (e.g. 1250 for NATURALGAS)
    futures_ltp: float
    ce: StraddleLeg
    pe: StraddleLeg
    net_credit_per_lot: float    # CE_ltp + PE_ltp
    estimated_sl_per_lot: float  # net_credit × STRADDLE_SL_MULTIPLIER
    sl_multiplier: float         # stored so report can show the multiplier
    net_credit_total: float      # per_lot × lot_units × quantity
    margin_required: float       # 0 when API unavailable
    margin_available: float
    margin_ok: bool
    spread_ok: bool
    all_ok: bool                 # spread_ok AND margin_ok
    block_reason: str | None     # human-readable reason when all_ok is False


# ── Pure helpers ──────────────────────────────────────────────────────────────

def round_to_atm_strike(price: float, interval: float) -> float:
    """Round a futures price to the nearest valid option strike interval."""
    return round(round(price / interval) * interval, 2)


def _threshold_abs(ltp: float, pct: float, tick_floor: float) -> float:
    """Max allowed spread in ₹ absolute, floored to one tick."""
    return max(ltp * pct / 100.0, tick_floor)


# ── Per-leg spread + delta check ──────────────────────────────────────────────

def _check_leg(
    tradingsymbol: str,
    instrument_token: int,
    strike: float,
    quote: dict,
    threshold_pct: float,
    futures_ltp: float,
    expiry: date,
    now_ist: datetime,
    settings: Settings,
    session: Session,
) -> StraddleLeg:
    ltp = float(quote.get("last_price", 0.0))
    depth = quote.get("depth", {})
    buy_d = depth.get("buy", [])
    sell_d = depth.get("sell", [])

    if ltp <= 0:
        return StraddleLeg(
            tradingsymbol=tradingsymbol, instrument_token=instrument_token, strike=strike,
            ltp=0.0, bid=0.0, ask=0.0, spread_abs=0.0, spread_pct=0.0,
            threshold_used=0.0, valid=False, rejection_reason="zero_ltp",
        )

    threshold = _threshold_abs(ltp, threshold_pct, NG_TICK_SIZE)

    if buy_d and sell_d:
        bid = float(buy_d[0]["price"])
        ask = float(sell_d[0]["price"])
        spread_abs = max(ask - bid, 0.0)
        spread_pct = (spread_abs / ltp * 100.0) if ltp > 0 else 999.0
        valid = spread_abs <= threshold
    else:
        bid = ask = ltp
        spread_abs = spread_pct = 0.0
        valid = True   # allow when depth unavailable; log warning
        log.warning("Straddle: no depth for %s — spread check skipped (allowing)", tradingsymbol)

    # Delta sanity via Black-76 (MCX options are priced against the futures)
    delta: float | None = None
    iv: float | None = None
    try:
        from app.greeks import compute_delta
        from app.storage import Instrument as _Instr
        inst = session.query(_Instr).filter(_Instr.instrument_token == instrument_token).first()
        if inst:
            close_h, close_m = settings.SESSION_CLOSE_MCX.split(":")
            expiry_close = datetime(
                expiry.year, expiry.month, expiry.day,
                int(close_h), int(close_m), tzinfo=IST,
            )
            t = max(0.0, (expiry_close - now_ist).total_seconds()) / _SECONDS_PER_YEAR
            gr = compute_delta(inst, ltp, futures_ltp, t)
            if gr.rejection_reason is None:
                delta = gr.delta
                iv = gr.iv
    except Exception as exc:
        log.warning("Straddle: delta compute failed for %s: %s", tradingsymbol, exc)

    return StraddleLeg(
        tradingsymbol=tradingsymbol, instrument_token=instrument_token, strike=strike,
        ltp=ltp, bid=bid, ask=ask,
        spread_abs=spread_abs, spread_pct=spread_pct, threshold_used=threshold,
        valid=valid, delta=delta, iv=iv,
    )


# ── Main orchestrator ─────────────────────────────────────────────────────────

def validate_straddle(
    underlying: str,
    quantity: int,
    kite_client: Any,
    session: Session,
    settings: Settings,
    now: datetime | None = None,
) -> StraddleValidation:
    """Run pre-execution straddle validation.

    Raises RuntimeError on hard failures (no instruments, no LTP).
    Returns StraddleValidation with all_ok=False for soft failures (bad spread/margin).
    """
    from app.expiry_resolver import NoEligibleExpiryError, resolve_expiry
    from app.storage import Instrument

    now_ist = now or datetime.now(IST)

    # ── 1. Expiry ─────────────────────────────────────────────────────────────
    try:
        resolved = resolve_expiry(
            underlying, session,
            instrument_type="CE",
            segment="MCX",
            now=now_ist,
            settings=settings,
        )
    except NoEligibleExpiryError as exc:
        raise RuntimeError(f"No eligible expiry for {underlying} options: {exc}") from exc

    expiry = resolved.expiry_date

    # ── 2. Near-month futures LTP → ATM strike ────────────────────────────────
    fut = (
        session.query(Instrument)
        .filter(
            Instrument.name == underlying,
            Instrument.instrument_type == "FUT",
            Instrument.expiry >= now_ist.date(),
        )
        .order_by(Instrument.expiry)
        .first()
    )
    if fut is None:
        raise RuntimeError(f"No near-month FUT instrument found for {underlying}")

    fut_key = f"MCX:{fut.tradingsymbol}"
    try:
        futures_ltp = float(kite_client.quote([fut_key])[fut_key]["last_price"])
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch {underlying} futures LTP: {exc}") from exc
    if futures_ltp <= 0:
        raise RuntimeError(f"{underlying} futures LTP is zero — market may be closed")

    atm_strike = round_to_atm_strike(futures_ltp, settings.STRADDLE_STRIKE_INTERVAL)

    # ── 3. Resolve CE + PE instruments ───────────────────────────────────────
    def _find(flag: str, strike: float) -> Instrument | None:
        return (
            session.query(Instrument)
            .filter(
                Instrument.name == underlying,
                Instrument.expiry == expiry,
                Instrument.instrument_type == flag,
                Instrument.strike == strike,
                Instrument.exchange == "MCX",
            )
            .first()
        )

    ce_instr = _find("CE", atm_strike)
    pe_instr = _find("PE", atm_strike)

    # Try adjacent strikes when exact ATM is absent from instruments CSV
    if ce_instr is None or pe_instr is None:
        iv = settings.STRADDLE_STRIKE_INTERVAL
        for mult in [1, -1, 2, -2, 3, -3]:
            adj = round(atm_strike + mult * iv, 2)
            if ce_instr is None:
                ce_instr = _find("CE", adj)
            if pe_instr is None:
                pe_instr = _find("PE", adj)
            if ce_instr is not None and pe_instr is not None:
                atm_strike = ce_instr.strike  # anchor to what we found
                break

    if ce_instr is None:
        raise RuntimeError(
            f"No CE instrument near strike {atm_strike} for {underlying} expiry {expiry}"
        )
    if pe_instr is None:
        raise RuntimeError(
            f"No PE instrument near strike {atm_strike} for {underlying} expiry {expiry}"
        )

    lot_size = ce_instr.lot_size
    lot_units = settings.MCX_LOT_UNITS.get(underlying, 1)
    total_qty = lot_size * quantity

    # ── 4. Option quotes + spread check ──────────────────────────────────────
    ce_key = f"MCX:{ce_instr.tradingsymbol}"
    pe_key = f"MCX:{pe_instr.tradingsymbol}"
    try:
        quotes = kite_client.quote([ce_key, pe_key])
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch option quotes: {exc}") from exc

    tpct = settings.STRADDLE_MAX_SPREAD_PCT
    ce_leg = _check_leg(
        ce_instr.tradingsymbol, ce_instr.instrument_token, ce_instr.strike,
        quotes.get(ce_key, {}), tpct, futures_ltp, expiry, now_ist, settings, session,
    )
    pe_leg = _check_leg(
        pe_instr.tradingsymbol, pe_instr.instrument_token, pe_instr.strike,
        quotes.get(pe_key, {}), tpct, futures_ltp, expiry, now_ist, settings, session,
    )

    # ── 5. Margin via basket_order_margins (with inter-leg netting) ───────────
    margin_required = margin_available = 0.0
    margin_ok = True
    try:
        basket = [
            {
                "exchange": "MCX", "tradingsymbol": ce_instr.tradingsymbol,
                "transaction_type": "SELL", "variety": "regular", "product": "NRML",
                "order_type": "MARKET", "quantity": total_qty, "price": 0,
            },
            {
                "exchange": "MCX", "tradingsymbol": pe_instr.tradingsymbol,
                "transaction_type": "SELL", "variety": "regular", "product": "NRML",
                "order_type": "MARKET", "quantity": total_qty, "price": 0,
            },
        ]
        md = kite_client.basket_order_margins(basket)
        if isinstance(md, dict):
            margin_required = float(md.get("total", 0.0))
        elif isinstance(md, list):
            margin_required = sum(float(m.get("total", 0)) for m in md)

        free = kite_client.margins()
        if isinstance(free, dict):
            commodity = free.get("commodity", {})
            margin_available = float(commodity.get("available", {}).get("cash", 0.0))

        margin_ok = margin_available >= margin_required
    except Exception as exc:
        log.warning("Straddle: margin API failed (%s) — margin guard skipped", exc)

    # ── 6. P&L summary ────────────────────────────────────────────────────────
    sl_mult = settings.STRADDLE_SL_MULTIPLIER
    net_credit_per_lot = ce_leg.ltp + pe_leg.ltp
    estimated_sl_per_lot = net_credit_per_lot * sl_mult
    net_credit_total = net_credit_per_lot * lot_units * quantity

    spread_ok = ce_leg.valid and pe_leg.valid
    all_ok = spread_ok and margin_ok

    block_reason: str | None = None
    if not margin_ok:
        block_reason = (
            f"Insufficient margin — need ₹{margin_required:,.0f}, "
            f"available ₹{margin_available:,.0f}"
        )
    elif not spread_ok:
        parts = []
        if not ce_leg.valid:
            parts.append(f"CE spread {ce_leg.spread_pct:.2f}% > {tpct:.1f}%")
        if not pe_leg.valid:
            parts.append(f"PE spread {pe_leg.spread_pct:.2f}% > {tpct:.1f}%")
        block_reason = "; ".join(parts) + " (Force Execute to override)"

    return StraddleValidation(
        underlying=underlying, atm_strike=atm_strike, expiry=expiry,
        quantity=quantity, lot_size=lot_size, lot_units=lot_units,
        futures_ltp=futures_ltp, ce=ce_leg, pe=pe_leg,
        net_credit_per_lot=net_credit_per_lot,
        estimated_sl_per_lot=estimated_sl_per_lot,
        sl_multiplier=sl_mult,
        net_credit_total=net_credit_total,
        margin_required=margin_required, margin_available=margin_available,
        margin_ok=margin_ok, spread_ok=spread_ok, all_ok=all_ok,
        block_reason=block_reason,
    )


# ── Report formatter ──────────────────────────────────────────────────────────

def build_validation_report(v: StraddleValidation) -> str:
    """Return the straddle validation report shown on the confirmation card."""
    def _leg_line(leg: StraddleLeg, label: str) -> str:
        status = "VALID" if leg.valid else "INVALID"
        delta_str = ""
        if leg.delta is not None:
            iv_str = f"  IV {leg.iv:.0%}" if leg.iv else ""
            delta_str = f"  δ={leg.delta:.3f}{iv_str}"
        return (
            f"   {label} ({leg.tradingsymbol}): "
            f"Bid {leg.bid:.2f} | Ask {leg.ask:.2f} | LTP {leg.ltp:.2f} "
            f"(Spread: {leg.spread_pct:.2f}%) → [{status}]{delta_str}"
        )

    margin_line = ""
    if v.margin_required > 0:
        m_ok = "OK" if v.margin_ok else "INSUFFICIENT"
        margin_line = (
            f"\n   Margin: ₹{v.margin_required:,.0f} required | "
            f"₹{v.margin_available:,.0f} available → [{m_ok}]"
        )

    action_line = (
        f"\n   ✅ Proceed? {v.quantity} lot — SELL CE + SELL PE simultaneously"
        if v.all_ok else
        f"\n   ⚠ Warning: {v.block_reason}"
    )

    return (
        f"STRADDLE VALIDATION REPORT\n"
        f"   {v.underlying}  ATM Strike: {v.atm_strike}  "
        f"Expiry: {v.expiry}  Futures LTP: {v.futures_ltp:.2f}\n"
        + _leg_line(v.ce, "CE")
        + "\n"
        + _leg_line(v.pe, "PE")
        + f"\n   Net Credit: ₹{v.net_credit_per_lot:.2f}/lot × {v.lot_units} units × "
        + f"{v.quantity} lot = ₹{v.net_credit_total:.0f}\n"
        + f"   Combined SL triggers at: ₹{v.estimated_sl_per_lot:.2f}/lot net loss "
        + f"({v.sl_multiplier:.1f}× net credit)"
        + margin_line
        + action_line
    )
