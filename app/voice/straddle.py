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

_OPTION_TICK_SIZE: float = 0.05     # minimum option premium tick (same for MCX and NFO)
_SECONDS_PER_YEAR: float = 365.25 * 24 * 3600
_PE_SEARCH_STEPS: int = 5           # scan ±5 strike intervals when delta-matching PE


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
    atm_strike: float          # CE anchor strike
    expiry: date
    quantity: int
    lot_size: int
    lot_units: int             # MCX_LOT_UNITS value (e.g. 1250 for NATURALGAS)
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


def _time_to_expiry_years(
    expiry: date, exchange: str, now_ist: datetime, settings: Settings
) -> float:
    """Return time to expiry as a fraction of a year."""
    session_close = settings.SESSION_CLOSE_MCX if exchange == "MCX" else settings.SESSION_CLOSE_NSE
    close_h, close_m = session_close.split(":")
    expiry_close = datetime(
        expiry.year, expiry.month, expiry.day,
        int(close_h), int(close_m), tzinfo=IST,
    )
    return max(0.0, (expiry_close - now_ist).total_seconds()) / _SECONDS_PER_YEAR


def _delta_for_instrument(
    inst: Any, ltp: float, futures_ltp: float, t: float
) -> float | None:
    """Compute call/put delta via Black-76. Returns None on any failure."""
    try:
        from app.greeks import compute_delta
        gr = compute_delta(inst, ltp, futures_ltp, t)
        if gr.rejection_reason is None:
            return gr.delta
    except Exception:
        pass
    return None


# ── Delta-matched PE selection ────────────────────────────────────────────────

def _select_pe_by_delta(
    ce_instr: Any,
    ce_quote: dict,
    pe_candidates: dict[str, Any],   # quote_key → Instrument
    quotes: dict,                     # full batch quote response
    futures_ltp: float,
    t: float,
    atm_strike: float,
    tolerance: float = 0.02,
) -> Any:
    """Return the PE instrument whose |delta| best matches |delta_CE|.

    If the ATM PE delta is already within *tolerance* of the CE delta,
    the ATM PE is returned unchanged (same-strike straddle).
    Falls back to ATM PE if delta computation is unavailable for any reason.
    """
    # Identify ATM PE instrument
    atm_pe: Any = None
    for inst in pe_candidates.values():
        if inst.strike == atm_strike:
            atm_pe = inst
            break
    if atm_pe is None:
        atm_pe = next(iter(pe_candidates.values()))  # closest available

    # Compute CE delta
    ce_ltp = float(ce_quote.get("last_price", 0.0))
    ce_delta = _delta_for_instrument(ce_instr, ce_ltp, futures_ltp, t) if ce_ltp > 0 else None
    if ce_delta is None:
        log.warning("_select_pe_by_delta: CE delta unavailable — using ATM PE")
        return atm_pe

    ce_delta_abs = abs(ce_delta)
    best_inst = atm_pe
    best_diff = float("inf")
    atm_diff: float | None = None

    for key, inst in pe_candidates.items():
        q = quotes.get(key, {})
        ltp = float(q.get("last_price", 0.0))
        if ltp <= 0:
            continue
        delta = _delta_for_instrument(inst, ltp, futures_ltp, t)
        if delta is None:
            continue
        diff = abs(abs(delta) - ce_delta_abs)
        if inst.strike == atm_strike:
            atm_diff = diff
        if diff < best_diff:
            best_diff = diff
            best_inst = inst

    # Prefer ATM when already within tolerance (simpler same-strike straddle)
    if atm_diff is not None and atm_diff <= tolerance:
        log.debug(
            "_select_pe_by_delta: ATM PE delta diff=%.3f ≤ %.2f — same strike",
            atm_diff, tolerance,
        )
        return atm_pe

    if best_diff < float("inf") and best_inst.strike != atm_strike:
        log.info(
            "Delta-matched PE: CE δ=%.3f → PE strike=%.1f (diff=%.3f vs ATM diff=%s)",
            ce_delta_abs, best_inst.strike, best_diff,
            f"{atm_diff:.3f}" if atm_diff is not None else "n/a",
        )
    return best_inst


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
    exchange: str = "MCX",
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

    threshold = _threshold_abs(ltp, threshold_pct, _OPTION_TICK_SIZE)

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

    # Delta via Black-76 (reuses pre-computed t from validate_straddle when available)
    delta: float | None = None
    iv: float | None = None
    try:
        from app.greeks import compute_delta
        from app.storage import Instrument as _Instr
        inst = session.query(_Instr).filter(_Instr.instrument_token == instrument_token).first()
        if inst:
            t = _time_to_expiry_years(expiry, exchange, now_ist, settings)
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
    exchange: str = "MCX",
) -> StraddleValidation:
    """Run pre-execution straddle validation.

    Raises RuntimeError on hard failures (no instruments, no LTP).
    Returns StraddleValidation with all_ok=False for soft failures (bad spread/margin).
    """
    from app.expiry_resolver import NoEligibleExpiryError, resolve_expiry
    from app.storage import Instrument

    now_ist = now or datetime.now(IST)

    # ── 1. Expiry ─────────────────────────────────────────────────────────────
    segment = "MCX" if exchange == "MCX" else "NFO"
    try:
        resolved = resolve_expiry(
            underlying, session,
            instrument_type="CE",
            segment=segment,
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

    fut_key = f"{fut.exchange}:{fut.tradingsymbol}"
    try:
        futures_ltp = float(kite_client.quote([fut_key])[fut_key]["last_price"])
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch {underlying} futures LTP: {exc}") from exc
    if futures_ltp <= 0:
        raise RuntimeError(f"{underlying} futures LTP is zero — market may be closed")

    strike_interval = settings.STRADDLE_STRIKE_INTERVALS.get(underlying, settings.STRADDLE_STRIKE_INTERVAL)
    atm_strike = round_to_atm_strike(futures_ltp, strike_interval)

    # ── 3. Resolve CE instrument (ATM, then adjacent) ─────────────────────────
    def _find(flag: str, strike: float) -> Instrument | None:
        return (
            session.query(Instrument)
            .filter(
                Instrument.name == underlying,
                Instrument.expiry == expiry,
                Instrument.instrument_type == flag,
                Instrument.strike == strike,
                Instrument.exchange == exchange,
            )
            .first()
        )

    ce_instr = _find("CE", atm_strike)
    if ce_instr is None:
        for mult in [1, -1, 2, -2, 3, -3]:
            ce_instr = _find("CE", round(atm_strike + mult * strike_interval, 2))
            if ce_instr is not None:
                atm_strike = ce_instr.strike   # anchor ATM to found CE strike
                break
    if ce_instr is None:
        raise RuntimeError(
            f"No CE instrument near strike {atm_strike} for {underlying} expiry {expiry}"
        )

    # ── 3b. Collect PE candidates (ATM ± _PE_SEARCH_STEPS intervals) ─────────
    pe_candidates: dict[str, Instrument] = {}   # quote_key → Instrument
    for i in range(_PE_SEARCH_STEPS + 1):
        for sign in ([0] if i == 0 else [1, -1]):
            s = round(atm_strike + sign * i * strike_interval, 2)
            inst = _find("PE", s)
            if inst is not None:
                key = f"{exchange}:{inst.tradingsymbol}"
                pe_candidates.setdefault(key, inst)

    if not pe_candidates:
        raise RuntimeError(
            f"No PE instrument near strike {atm_strike} for {underlying} expiry {expiry}"
        )

    lot_size = ce_instr.lot_size
    # MCX: lot_size=1 in CSV, real units in MCX_LOT_UNITS. NFO: lot_size IS the real lot size.
    lot_units = settings.MCX_LOT_UNITS.get(underlying, lot_size)
    total_qty = lot_size * quantity

    # ── 4. Batch-quote CE + all PE candidates (single API call) ──────────────
    ce_key = f"{exchange}:{ce_instr.tradingsymbol}"
    all_keys = [ce_key] + list(pe_candidates.keys())
    try:
        quotes = kite_client.quote(all_keys)
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch option quotes: {exc}") from exc

    # ── 4b. Pick delta-matched PE leg ─────────────────────────────────────────
    t = _time_to_expiry_years(expiry, exchange, now_ist, settings)
    pe_instr = _select_pe_by_delta(
        ce_instr=ce_instr,
        ce_quote=quotes.get(ce_key, {}),
        pe_candidates=pe_candidates,
        quotes=quotes,
        futures_ltp=futures_ltp,
        t=t,
        atm_strike=atm_strike,
        tolerance=0.02,
    )
    pe_key = f"{exchange}:{pe_instr.tradingsymbol}"

    # ── 5. Spread check ───────────────────────────────────────────────────────
    tpct = settings.STRADDLE_MAX_SPREAD_PCT
    ce_leg = _check_leg(
        ce_instr.tradingsymbol, ce_instr.instrument_token, ce_instr.strike,
        quotes.get(ce_key, {}), tpct, futures_ltp, expiry, now_ist, settings, session, exchange,
    )
    pe_leg = _check_leg(
        pe_instr.tradingsymbol, pe_instr.instrument_token, pe_instr.strike,
        quotes.get(pe_key, {}), tpct, futures_ltp, expiry, now_ist, settings, session, exchange,
    )

    # ── 6. Margin via basket_order_margins (with inter-leg netting) ───────────
    margin_required = margin_available = 0.0
    margin_ok = True
    try:
        basket = [
            {
                "exchange": exchange, "tradingsymbol": ce_instr.tradingsymbol,
                "transaction_type": "SELL", "variety": "regular", "product": "NRML",
                "order_type": "MARKET", "quantity": total_qty, "price": 0,
            },
            {
                "exchange": exchange, "tradingsymbol": pe_instr.tradingsymbol,
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
            margin_segment = free.get("commodity" if exchange == "MCX" else "equity", {})
            margin_available = float(margin_segment.get("available", {}).get("cash", 0.0))

        margin_ok = margin_available >= margin_required
    except Exception as exc:
        log.warning("Straddle: margin API failed (%s) — margin guard skipped", exc)

    # ── 7. P&L summary ────────────────────────────────────────────────────────
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

    # Show separate strikes in the header when delta-matching picked different strikes
    if v.ce.strike != v.pe.strike:
        strike_line = f"CE Strike: {v.ce.strike}  PE Strike: {v.pe.strike} (delta-matched)"
    else:
        strike_line = f"ATM Strike: {v.atm_strike}"

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
        f"   {v.underlying}  {strike_line}  "
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
