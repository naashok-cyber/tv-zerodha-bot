"""NATURALGAS delta-hedge cron job.

Fires every 1 min during MCX hours. On each tick:
  1. Pull live MCX positions for NATURALGAS option legs
  2. Compute net portfolio delta (Black-76) using front-month futures as F
  3. If |net δ| > NG_DELTA_HEDGE_THRESHOLD mmBtu, SELL more lots on the
     nearest-ATM existing strike on the deficit side (CE if long-biased,
     PE if short-biased).
  4. Lots: 1 if threshold < |δ| ≤ 2× threshold, else 2.
  5. Order: LIMIT @ best bid → escalate to MARKET after
     NG_DELTA_HEDGE_LIMIT_WAIT_SEC if unfilled.
  6. Increment the matching Position.quantity so the 23:20 straddle squareoff
     closes the full broker-side quantity.
  7. Telegram-notify the action.
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import datetime, time as dtime
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

_LOG_PREFIX = "[ng_hedge]"
_HALFEXIT_PREFIX = "[ng_halfexit]"
_LADDER_PREFIX = "[ng_ladder]"
_BNF_SL_PREFIX = "[bnf_sl]"
_UNDERLYING = "NATURALGAS"
_BNF_UNDERLYING = "BANKNIFTY"

# Pre-hedge: fire early when order-book imbalance signals continued drift.
# Activates when |δ| > 60% of threshold AND imbalance confirms direction.
_PRE_HEDGE_DELTA_PCT         = 0.60   # 60% of threshold → check imbalance
_PRE_HEDGE_IMBALANCE_THRESH  = 0.50   # |imbalance| > 0.50 → lower eff. threshold

# ATM corridor: when the nearest existing hedge leg is >N pts from current
# futures price, open a fresh ATM position instead of adding to the OTM leg.
# OTM legs at delta~0.15 need 3× more lots to move the same delta as ATM~0.50.
_ATM_CORRIDOR_PTS = 10.0

# Delta efficiency: even within the corridor, if the nearest existing leg has
# |δ| < this threshold, open a fresh ATM position instead. At δ<0.40 the option
# is too OTM — ATM (~0.50) achieves the same hedge in fewer lots with more theta.
_MIN_HEDGE_DELTA = 0.40

# ADX-based dynamic hedge threshold.
# In a range-bound session (ADX < 20) the delta oscillates naturally and hedging
# every ±350 mmBtu whipsaws the book. Widen the band to let theta do the work.
_ADX_PERIOD          = 14
_ADX_CANDLE_INTERVAL = "10minute"
_ADX_CHOPPY_MAX      = 20.0    # ADX < 20  → choppy/range-bound → wide band
_ADX_TREND_MIN       = 25.0    # ADX > 25  → trending            → tight band
_ADX_BAND_CHOPPY     = 700.0   # mmBtu (= ₹700/₹1 NG move) when choppy
_ADX_BAND_MILD       = 500.0   # mmBtu when mild trend (ADX 20–25)
_ADX_BAND_TREND      = 350.0   # mmBtu when trending


def _adx_threshold(
    kite: Any, futures_inst: Any, fallback: float
) -> tuple[float, float | None, str]:
    """Compute ADX on 10-min NG futures candles and return (threshold_mmBtu, adx, regime).

    ADX < 20  → CHOPPY   → ±700 mmBtu band (fewer trades in range-bound market)
    ADX 20–25 → MILD     → ±500 mmBtu band
    ADX > 25  → TRENDING → ±350 mmBtu band (hedge promptly in strong moves)

    Falls back to (fallback, None, "FALLBACK") on any API or compute error.
    """
    from datetime import timedelta

    from app.adx import compute_adx
    from app.config import IST as _IST

    n_candles = _ADX_PERIOD * 3 + 5
    now = datetime.now(_IST)
    from_dt = now - timedelta(minutes=n_candles * 10 + 60)
    try:
        raw = kite.historical_data(
            futures_inst.instrument_token, from_dt, now, _ADX_CANDLE_INTERVAL
        )
    except Exception as exc:
        log.warning(
            "%s ADX: historical_data failed: %s — using fallback threshold %.0f",
            _LOG_PREFIX, exc, fallback,
        )
        return fallback, None, "FALLBACK"

    adx = compute_adx(raw, period=_ADX_PERIOD)
    if adx is None:
        log.warning(
            "%s ADX: insufficient candles (got %d) — using fallback threshold %.0f",
            _LOG_PREFIX, len(raw) if raw else 0, fallback,
        )
        return fallback, None, "FALLBACK"

    if adx < _ADX_CHOPPY_MAX:
        band, regime = _ADX_BAND_CHOPPY, "CHOPPY"
    elif adx < _ADX_TREND_MIN:
        band, regime = _ADX_BAND_MILD, "MILD"
    else:
        band, regime = _ADX_BAND_TREND, "TRENDING"

    log.info(
        "%s ADX(14, 10min)=%.1f → %s → threshold=±%.0f mmBtu",
        _LOG_PREFIX, adx, regime, band,
    )
    return band, adx, regime


def _telegram_notify(settings: Any, text: str) -> None:
    """Best-effort Telegram send. No-op if creds missing."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as exc:
        log.warning("%s telegram notify failed: %s", _LOG_PREFIX, exc)


_IMBALANCE_TOP5_WEIGHT  = 0.60   # weight for top-5 depth imbalance (immediate pressure)
_IMBALANCE_FULL_WEIGHT  = 0.40   # weight for full order-book imbalance (structural sentiment)


def _get_futures_imbalance(kite: Any, fut_full: str) -> float | None:
    """Combined bid-ask imbalance: 60% top-5 depth + 40% full order book.
    Top-5 captures immediate price pressure; full book captures structural sentiment.
    Returns None on API error or zero total quantity."""
    try:
        q = kite.quote([fut_full]).get(fut_full, {})

        # Top-5 depth imbalance
        depth   = q.get("depth") or {}
        buys    = depth.get("buy")  or []
        sells   = depth.get("sell") or []
        t5_bid  = sum(b.get("quantity", 0) for b in buys)
        t5_ask  = sum(s.get("quantity", 0) for s in sells)
        t5_total = t5_bid + t5_ask
        t5_imb   = (t5_bid - t5_ask) / t5_total if t5_total > 0 else 0.0

        # Full order-book imbalance (buy_quantity / sell_quantity from Kite)
        fb_bid   = int(q.get("buy_quantity")  or 0)
        fb_ask   = int(q.get("sell_quantity") or 0)
        fb_total = fb_bid + fb_ask
        fb_imb   = (fb_bid - fb_ask) / fb_total if fb_total > 0 else 0.0

        if t5_total == 0 and fb_total == 0:
            return None

        combined = _IMBALANCE_TOP5_WEIGHT * t5_imb + _IMBALANCE_FULL_WEIGHT * fb_imb
        log.info(
            "%s imbalance: top5=%+.3f (bid=%d ask=%d) "
            "full=%+.3f (bid=%d ask=%d) → combined=%+.3f",
            _LOG_PREFIX, t5_imb, t5_bid, t5_ask, fb_imb, fb_bid, fb_ask, combined,
        )
        return combined
    except Exception as exc:
        log.warning("%s imbalance fetch failed: %s", _LOG_PREFIX, exc)
        return None


def _is_insufficient_funds(exc: BaseException) -> bool:
    return "Insufficient funds" in str(exc)


def _limit_then_market(
    kite: Any,
    instrument: Any,
    side: str,                  # "BUY" or "SELL"
    qty: int,
    settings: Any,
) -> tuple[str | None, str]:
    """LIMIT at top-of-book for `side` (best bid for SELL, best ask for BUY);
    cancel + MARKET if unfilled within NG_DELTA_HEDGE_LIMIT_WAIT_SEC.

    Returns (kite_order_id, mode):
      mode ∈ {"LIMIT", "MARKET", "INSUFFICIENT_FUNDS", "FAILED"}.
    The INSUFFICIENT_FUNDS mode lets the caller try a margin-cheaper fallback.
    """
    from app.orders import backoff_call, place_entry

    sym = instrument.tradingsymbol
    full = f"{instrument.exchange}:{sym}"
    book_side = "buy" if side == "SELL" else "sell"  # we hit the opposite book

    price: float | None = None
    try:
        q = kite.quote([full])[full]
        levels = q.get("depth", {}).get(book_side, []) or []
        if levels:
            price = float(levels[0].get("price") or 0) or None
        if price is None or price <= 0:
            price = float(q.get("last_price") or 0) or None
    except Exception as exc:
        log.warning("%s quote failed for %s: %s", _LOG_PREFIX, sym, exc)

    if price is None or price <= 0:
        log.info("%s no %s book for %s — placing MARKET", _LOG_PREFIX, book_side, sym)
        try:
            oid = place_entry(kite, instrument, side, qty, "MARKET", "NRML")
            return (oid, "MARKET") if oid else (None, "FAILED")
        except Exception as exc:
            log.error("%s %s MARKET failed for %s: %s", _LOG_PREFIX, side, sym, exc)
            if _is_insufficient_funds(exc):
                return (None, "INSUFFICIENT_FUNDS")
            return (None, "FAILED")

    try:
        oid = place_entry(
            kite, instrument, side, qty, "LIMIT", "NRML",
            price=Decimal(str(price)),
        )
    except Exception as exc:
        log.error("%s %s LIMIT failed for %s: %s", _LOG_PREFIX, side, sym, exc)
        if _is_insufficient_funds(exc):
            return (None, "INSUFFICIENT_FUNDS")
        return (None, "FAILED")
    if oid is None:
        return (None, "FAILED")

    deadline = _time.monotonic() + settings.NG_DELTA_HEDGE_LIMIT_WAIT_SEC
    while _time.monotonic() < deadline:
        _time.sleep(2)
        try:
            history = backoff_call(kite.order_history, oid)
        except Exception:
            continue
        if not history:
            continue
        latest = history[-1]
        status = (latest.get("status") or "").upper()
        if status == "COMPLETE":
            log.info("%s %s LIMIT %s filled @ %s", _LOG_PREFIX, side, oid, latest.get("average_price"))
            return (oid, "LIMIT")
        if status in ("CANCELLED", "REJECTED"):
            log.warning("%s %s LIMIT %s ended %s — escalating", _LOG_PREFIX, side, oid, status)
            break

    try:
        backoff_call(kite.cancel_order, variety="regular", order_id=oid)
        log.info("%s cancelled stuck LIMIT %s", _LOG_PREFIX, oid)
    except Exception as exc:
        log.warning("%s cancel %s failed (proceeding to MARKET anyway): %s", _LOG_PREFIX, oid, exc)

    try:
        m_oid = place_entry(kite, instrument, side, qty, "MARKET", "NRML")
        if m_oid:
            log.info("%s %s MARKET escalation %s for %s", _LOG_PREFIX, side, m_oid, sym)
            return (m_oid, "MARKET")
    except Exception as exc:
        log.error("%s %s MARKET escalation failed for %s: %s", _LOG_PREFIX, side, sym, exc)
        if _is_insufficient_funds(exc):
            return (None, "INSUFFICIENT_FUNDS")

    return (None, "FAILED")


def _pick_atm_strike(session: Any, expiry: Any, opt_type: str, F: float) -> Any:
    """Return Instrument for the strike nearest to F on the given side (CE/PE),
    same expiry as the open book. None if no candidate found."""
    from app.storage import Instrument
    candidates = (
        session.query(Instrument)
        .filter(
            Instrument.name == _UNDERLYING,
            Instrument.instrument_type == opt_type,
            Instrument.exchange == "MCX",
            Instrument.expiry == expiry,
        )
        .all()
    )
    if not candidates:
        return None
    return min(candidates, key=lambda inst: abs(float(inst.strike or 0) - F))


def _pick_itm_strike(session: Any, expiry: Any, opt_type: str, F: float) -> Any:
    """Return Instrument for the nearest ITM strike for opt_type at futures price F.
    ITM CE: nearest strike strictly below F (CE goes ITM when F > strike).
    ITM PE: nearest strike strictly above F (PE goes ITM when F < strike).
    Returns None if no ITM candidate found in the instruments table."""
    from app.storage import Instrument
    candidates = (
        session.query(Instrument)
        .filter(
            Instrument.name == _UNDERLYING,
            Instrument.instrument_type == opt_type,
            Instrument.exchange == "MCX",
            Instrument.expiry == expiry,
        )
        .all()
    )
    if not candidates:
        return None
    if opt_type == "CE":
        itm = [c for c in candidates if float(c.strike or 0) < F]
    else:
        itm = [c for c in candidates if float(c.strike or 0) > F]
    if not itm:
        return None
    return min(itm, key=lambda c: abs(float(c.strike or 0) - F))


def _maybe_half_exit(settings: Any, session: Any, kite: Any, positions: list) -> bool:
    """One-shot profit lock: when unrealised P&L on all NG legs >= trigger,
    BUY back ceil(qty/2) lots of every short leg (options + futures) at LIMIT
    @ ask, escalating to MARKET. Writes a flag file so it never repeats until
    re-armed by deleting the flag.

    `positions` is the same kite.positions()['net'] list the delta-hedge job
    already fetched — avoids a second API call.
    """
    from app.storage import Instrument

    if not getattr(settings, "NG_HALF_EXIT_ENABLED", False):
        return False

    flag_path = settings.NG_HALF_EXIT_FLAG_PATH
    if os.path.exists(flag_path):
        log.debug("%s flag present at %s — already done", _HALFEXIT_PREFIX, flag_path)
        return False

    ng = [
        p for p in positions
        if "NATURALGAS" in (p.get("tradingsymbol") or "")
        and p.get("quantity", 0) != 0
    ]
    if not ng:
        return False

    # m2m today = P&L change since previous close. "Today's base" is m2m=0;
    # cross +trigger to fire. Independent of historical entry-cost P&L.
    total_m2m = sum(float(p.get("m2m", 0.0)) for p in ng)
    total_unr = sum(float(p.get("unrealised", 0.0)) for p in ng)
    trigger = float(settings.NG_HALF_EXIT_PNL_TRIGGER)
    log.info(
        "%s today_m2m=%+.0f unrealised=%+.0f trigger=+%.0f legs=%d",
        _HALFEXIT_PREFIX, total_m2m, total_unr, trigger, len(ng),
    )
    if total_m2m < trigger:
        return False

    if settings.DRY_RUN:
        log.info("%s DRY_RUN — would close half of each NG leg", _HALFEXIT_PREFIX)
        return False

    fired: list[tuple[str, int, str | None, str]] = []
    for p in ng:
        sym = p["tradingsymbol"]
        qty_signed = int(p["quantity"])
        abs_qty = abs(qty_signed)
        # Round UP for odd lots (e.g. 9 → close 5, 1 → close 1)
        close_qty = (abs_qty + 1) // 2
        if close_qty <= 0:
            continue
        # All current NG legs are SHORT → close direction is BUY.
        # Defensive: if a leg were long, we'd SELL to halve instead.
        side = "BUY" if qty_signed < 0 else "SELL"

        inst = session.query(Instrument).filter_by(tradingsymbol=sym).first()
        if inst is None:
            log.warning("%s no instrument for %s — skip", _HALFEXIT_PREFIX, sym)
            continue
        oid, mode = _limit_then_market(kite, inst, side, close_qty, settings)
        fired.append((sym, close_qty, oid, mode))
        if oid:
            log.info("%s %s %d %s (%s) → oid=%s",
                     _HALFEXIT_PREFIX, side, close_qty, sym, mode, oid)
        else:
            log.error("%s %s %d %s FAILED (%s)",
                      _HALFEXIT_PREFIX, side, close_qty, sym, mode)

    # Write the flag even on partial success so the user is alerted and any
    # missed legs can be handled manually; re-arm by deleting the file.
    try:
        os.makedirs(os.path.dirname(flag_path) or ".", exist_ok=True)
        with open(flag_path, "w") as f:
            f.write(f"fired_at={datetime.now().isoformat()}\n")
            f.write(f"trigger_pnl={total_unr:.2f}\n")
            for sym, qty, oid, mode in fired:
                f.write(f"  {sym} qty={qty} oid={oid} mode={mode}\n")
    except Exception as exc:
        log.error("%s failed to write flag %s: %s", _HALFEXIT_PREFIX, flag_path, exc)

    ok = [x for x in fired if x[2] is not None]
    failed = [x for x in fired if x[2] is None]
    _telegram_notify(
        settings,
        f"NG half-exit fired (P&L=+₹{total_unr:,.0f})\n"
        + "\n".join(f"  BUY {q} {s}" for s, q, _, _ in ok)
        + (f"\nFAILED: {[s for s,_,_,_ in failed]}" if failed else "")
        + f"\nRe-arm: rm {flag_path}",
    )
    log.info("%s DONE — %d ok, %d failed", _HALFEXIT_PREFIX, len(ok), len(failed))

    # Seed the straddle ladder state: baseline = today's m2m at fire time.
    # Subsequent ticks close 1 ATM straddle each time m2m rises another step.
    ladder_state = {
        "baseline_m2m": total_m2m,
        "lots_closed": 0,
        "fired_at": datetime.now().isoformat(),
    }
    ladder_path = getattr(settings, "NG_STRADDLE_LADDER_STATE_PATH", "")
    if ladder_path:
        try:
            os.makedirs(os.path.dirname(ladder_path) or ".", exist_ok=True)
            with open(ladder_path, "w") as f:
                json.dump(ladder_state, f, indent=2)
            log.info("%s ladder baseline=%.0f written to %s",
                     _HALFEXIT_PREFIX, total_m2m, ladder_path)
        except Exception as exc:
            log.error("%s failed to write ladder state: %s", _HALFEXIT_PREFIX, exc)
    return True


def _maybe_bnf_stop_loss(settings: Any, session: Any, kite: Any, positions: list) -> bool:
    """One-shot BANKNIFTY stop-loss: when today's m2m on all BNF legs (open +
    closed) reaches BNF_STOP_LOSS_TRIGGER (negative), BUY back ALL open BNF
    shorts at LIMIT @ ask → escalate to MARKET. Writes a flag so it doesn't
    repeat. Delete the flag to re-arm.

    Each tick logs total m2m + unrealised so the user can see drift even when
    the trigger hasn't fired.
    """
    from app.storage import Instrument

    if not getattr(settings, "BNF_STOP_LOSS_ENABLED", False):
        return False

    flag_path = settings.BNF_STOP_LOSS_FLAG_PATH
    if os.path.exists(flag_path):
        return False

    bnf = [
        p for p in positions
        if _BNF_UNDERLYING in (p.get("tradingsymbol") or "")
    ]
    if not bnf:
        return False

    # m2m today: sum across ALL BNF legs (open + already-closed today).
    # unrealised: open + realized P&L from closes earlier in life of position.
    total_m2m = sum(float(p.get("m2m", 0.0)) for p in bnf)
    total_unr = sum(float(p.get("unrealised", 0.0)) for p in bnf)
    trigger = float(settings.BNF_STOP_LOSS_TRIGGER)
    open_legs = [p for p in bnf if int(p.get("quantity", 0)) != 0]
    log.info(
        "%s today_m2m=%+.0f unrealised=%+.0f trigger=%+.0f open=%d total_legs=%d",
        _BNF_SL_PREFIX, total_m2m, total_unr, trigger, len(open_legs), len(bnf),
    )
    if total_m2m > trigger:
        return False

    if settings.DRY_RUN:
        log.info("%s DRY_RUN — would close all open BNF shorts", _BNF_SL_PREFIX)
        return False

    fired: list[tuple[str, str, int, str | None, str]] = []
    for p in open_legs:
        sym = p["tradingsymbol"]
        qty_signed = int(p["quantity"])
        abs_qty = abs(qty_signed)
        side = "BUY" if qty_signed < 0 else "SELL"
        inst = session.query(Instrument).filter_by(tradingsymbol=sym).first()
        if inst is None:
            log.warning("%s no instrument for %s — skip", _BNF_SL_PREFIX, sym)
            continue
        log.info("%s closing: %s %d %s (was qty=%+d)",
                 _BNF_SL_PREFIX, side, abs_qty, sym, qty_signed)
        oid, mode = _limit_then_market(kite, inst, side, abs_qty, settings)
        fired.append((sym, side, abs_qty, oid, mode))
        if oid is None:
            log.error("%s %s %d %s FAILED (%s)", _BNF_SL_PREFIX, side, abs_qty, sym, mode)

    try:
        os.makedirs(os.path.dirname(flag_path) or ".", exist_ok=True)
        with open(flag_path, "w") as f:
            f.write(f"fired_at={datetime.now().isoformat()}\n")
            f.write(f"trigger_m2m={total_m2m:.2f} trigger_threshold={trigger:.2f}\n")
            for sym, side, qty, oid, mode in fired:
                f.write(f"  {sym} {side} {qty} oid={oid} mode={mode}\n")
    except Exception as exc:
        log.error("%s failed to write flag %s: %s", _BNF_SL_PREFIX, flag_path, exc)

    ok = [x for x in fired if x[3] is not None]
    failed = [x for x in fired if x[3] is None]
    _telegram_notify(
        settings,
        f"BNF stop-loss fired (today m2m=₹{total_m2m:,.0f})\n"
        + "\n".join(f"  {side} {q} {s}" for s, side, q, _, _ in ok)
        + (f"\nFAILED: {[s for s,_,_,_,_ in failed]}" if failed else "")
        + f"\nRe-arm: rm {flag_path}",
    )
    log.info("%s DONE — %d ok, %d failed", _BNF_SL_PREFIX, len(ok), len(failed))
    return True


def _maybe_straddle_ladder(settings: Any, session: Any, kite: Any, positions: list) -> bool:
    """After the half-exit fires, close 1 ATM straddle (1 short CE + 1 short PE
    nearest F) each time today's m2m rises another NG_STRADDLE_LADDER_STEP above
    the baseline recorded at half-exit fire time. One rung per tick.

    State file (JSON): {baseline_m2m, lots_closed, ...}.
    Delete the state file (and the half-exit flag) to fully re-arm.
    """
    from app.storage import Instrument

    if not getattr(settings, "NG_STRADDLE_LADDER_ENABLED", False):
        return False
    state_path = settings.NG_STRADDLE_LADDER_STATE_PATH
    if not os.path.exists(state_path):
        return False

    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception as exc:
        log.error("%s failed to read state %s: %s", _LADDER_PREFIX, state_path, exc)
        return False

    baseline = float(state.get("baseline_m2m", 0.0))
    lots_closed = int(state.get("lots_closed", 0))
    step = float(settings.NG_STRADDLE_LADDER_STEP)
    # first_step lets the first rung differ from subsequent rungs
    # (e.g. first at +₹7k then +₹2k each after). Defaults to step.
    first_step = float(state.get("first_step", step))

    ng = [
        p for p in positions
        if "NATURALGAS" in (p.get("tradingsymbol") or "")
        and p.get("quantity", 0) != 0
        and ((p["tradingsymbol"]).endswith("CE") or (p["tradingsymbol"]).endswith("PE"))
    ]
    if not ng:
        return False

    total_m2m = sum(float(p.get("m2m", 0.0)) for p in ng)
    # Rung 0 at baseline + first_step; subsequent rungs at baseline + first_step + N*step
    next_trigger = baseline + first_step + lots_closed * step
    log.info(
        "%s m2m=%+.0f baseline=%+.0f first_step=%+.0f step=%+.0f lots_closed=%d next_trigger=%+.0f",
        _LADDER_PREFIX, total_m2m, baseline, first_step, step, lots_closed, next_trigger,
    )
    if total_m2m < next_trigger:
        return False

    # Front-month futures for F
    futures = (
        session.query(Instrument)
        .filter(
            Instrument.name == _UNDERLYING,
            Instrument.instrument_type == "FUT",
            Instrument.exchange == "MCX",
            Instrument.expiry != None,  # noqa: E711
        )
        .order_by(Instrument.expiry.asc())
        .first()
    )
    if futures is None:
        log.warning("%s no futures instrument — skip", _LADDER_PREFIX)
        return False
    try:
        fut_ltp = kite.ltp([f"MCX:{futures.tradingsymbol}"])[f"MCX:{futures.tradingsymbol}"]["last_price"]
    except Exception as exc:
        log.warning("%s ltp failed: %s", _LADDER_PREFIX, exc)
        return False
    F = float(fut_ltp)

    # Pick nearest-ATM SHORT CE + SHORT PE from currently open legs
    ce_legs = [p for p in ng if p["tradingsymbol"].endswith("CE") and p["quantity"] < 0]
    pe_legs = [p for p in ng if p["tradingsymbol"].endswith("PE") and p["quantity"] < 0]
    if not ce_legs or not pe_legs:
        log.warning("%s no CE or no PE short legs — skip", _LADDER_PREFIX)
        return False

    def _strike(p):
        inst = session.query(Instrument).filter_by(tradingsymbol=p["tradingsymbol"]).first()
        return float(inst.strike) if inst and inst.strike else 0.0

    ce_legs.sort(key=lambda p: abs(_strike(p) - F))
    pe_legs.sort(key=lambda p: abs(_strike(p) - F))
    ce_sym = ce_legs[0]["tradingsymbol"]
    pe_sym = pe_legs[0]["tradingsymbol"]
    ce_inst = session.query(Instrument).filter_by(tradingsymbol=ce_sym).first()
    pe_inst = session.query(Instrument).filter_by(tradingsymbol=pe_sym).first()

    if settings.DRY_RUN:
        log.info("%s DRY_RUN — would BUY 1 %s + BUY 1 %s", _LADDER_PREFIX, ce_sym, pe_sym)
        return False

    log.info("%s HEDGE straddle exit #%d: BUY 1 %s + BUY 1 %s (m2m=+₹%.0f, trigger=+₹%.0f)",
             _LADDER_PREFIX, lots_closed + 1, ce_sym, pe_sym, total_m2m, next_trigger)

    ce_oid, ce_mode = _limit_then_market(kite, ce_inst, "BUY", 1, settings)
    pe_oid, pe_mode = _limit_then_market(kite, pe_inst, "BUY", 1, settings)

    fires = state.get("fires", [])
    fires.append({
        "at": datetime.now().isoformat(),
        "trigger_m2m": total_m2m,
        "ce_sym": ce_sym, "ce_oid": ce_oid, "ce_mode": ce_mode,
        "pe_sym": pe_sym, "pe_oid": pe_oid, "pe_mode": pe_mode,
    })
    state["lots_closed"] = lots_closed + 1
    state["fires"] = fires

    try:
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        log.error("%s failed to save state: %s", _LADDER_PREFIX, exc)

    _telegram_notify(
        settings,
        f"NG ladder exit #{lots_closed + 1} @ m2m=+₹{total_m2m:,.0f}\n"
        f"  BUY 1 {ce_sym} ({ce_mode}) oid={ce_oid}\n"
        f"  BUY 1 {pe_sym} ({pe_mode}) oid={pe_oid}\n"
        f"  next trigger: +₹{baseline + first_step + (lots_closed + 1) * step:,.0f}"
    )
    return True


def run_delta_hedge_job(settings: Any, session_factory: Any) -> None:
    """Single tick of the NG delta-hedge loop. Called by APScheduler cron."""
    from app import state
    from app.config import IST
    from app.greeks import compute_delta
    from app.kite_session import get_session_manager
    from app.storage import ClosedTrade, Instrument, Position

    if not state.is_ng_hedge_enabled(settings.NG_DELTA_HEDGE_ENABLED):
        log.info("%s disabled via /control — skipping", _LOG_PREFIX)
        return
    if state.is_emergency_stop():
        log.info("%s emergency stop — skipping", _LOG_PREFIX)
        return
    if state.get_session_invalid():
        log.info("%s session invalid — skipping", _LOG_PREFIX)
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.warning("%s kite client unavailable: %s", _LOG_PREFIX, exc)
        return

    try:
        positions = kite.positions().get("net", []) or []
    except Exception as exc:
        log.warning("%s kite.positions() failed: %s", _LOG_PREFIX, exc)
        return

    ng_legs = [
        p for p in positions
        if p.get("exchange") == "MCX"
        and p.get("quantity", 0) != 0
        and _UNDERLYING in (p.get("tradingsymbol") or "")
        and ((p["tradingsymbol"]).endswith("CE") or (p["tradingsymbol"]).endswith("PE"))
    ]
    # Futures legs are included in delta sum (δ=+1 per unit) but never hedged into.
    ng_futures = [
        p for p in positions
        if p.get("exchange") == "MCX"
        and p.get("quantity", 0) != 0
        and _UNDERLYING in (p.get("tradingsymbol") or "")
        and (p["tradingsymbol"]).endswith("FUT")
    ]
    if not ng_legs and not ng_futures:
        log.debug("%s no open NG legs — nothing to hedge", _LOG_PREFIX)
        return

    with session_factory() as session:
        # Resolve options expiry from any leg's instrument row.
        # If only futures are open (no options), there's no delta drift to chase.
        if not ng_legs:
            log.debug("%s only futures positions (no options) — nothing to rebalance", _LOG_PREFIX)
            return
        sample = session.query(Instrument).filter_by(
            tradingsymbol=ng_legs[0]["tradingsymbol"]
        ).first()
        if sample is None or sample.expiry is None:
            log.warning("%s no instrument row for %s — skipping", _LOG_PREFIX, ng_legs[0]["tradingsymbol"])
            return
        opt_expiry = sample.expiry

        # Front-month futures for F
        futures = (
            session.query(Instrument)
            .filter(
                Instrument.name == _UNDERLYING,
                Instrument.instrument_type == "FUT",
                Instrument.exchange == "MCX",
                Instrument.expiry != None,  # noqa: E711
            )
            .order_by(Instrument.expiry.asc())
            .first()
        )
        if futures is None:
            log.warning("%s no futures instrument — skipping", _LOG_PREFIX)
            return
        fut_full = f"MCX:{futures.tradingsymbol}"

        # Batch-fetch LTPs (1 call: futures + all option legs)
        leg_fulls = [f"MCX:{p['tradingsymbol']}" for p in ng_legs]
        try:
            ltp_resp = kite.ltp([fut_full] + leg_fulls)
        except Exception as exc:
            log.warning("%s kite.ltp() failed: %s", _LOG_PREFIX, exc)
            return

        fut_ltp = ltp_resp.get(fut_full, {}).get("last_price")
        if not fut_ltp or fut_ltp <= 0:
            log.warning("%s futures LTP missing — skipping", _LOG_PREFIX)
            return
        F = float(fut_ltp)

        # Time to expiry: MCX session closes at SESSION_CLOSE_MCX IST
        exp_h, exp_m = (int(x) for x in settings.SESSION_CLOSE_MCX.split(":"))
        expiry_dt = datetime.combine(opt_expiry, dtime(exp_h, exp_m), tzinfo=IST)
        now = datetime.now(IST)
        T = (expiry_dt - now).total_seconds() / 31_557_600.0
        if T <= (1.0 / 365):  # < 1 day: don't add naked premium so close to expiry
            log.info("%s T=%.4fy (<1d) — skipping hedge near expiry", _LOG_PREFIX, T)
            return

        lot_mult = settings.MCX_LOT_UNITS.get(_UNDERLYING, 1250)
        per_leg: list[tuple[str, Any, int, float, float]] = []  # (sym, inst, qty, delta_per_unit, pos_delta)
        total_delta = 0.0

        # Add futures legs first (δ=+1 per unit). Never hedged into, only counted.
        futures_delta = 0.0
        for fp in ng_futures:
            f_delta = fp["quantity"] * 1.0 * lot_mult
            futures_delta += f_delta
            log.info(
                "%s futures leg: %s qty=%+d → δ=%+.0f mmBtu",
                _LOG_PREFIX, fp["tradingsymbol"], fp["quantity"], f_delta,
            )
        total_delta += futures_delta

        for p in ng_legs:
            sym = p["tradingsymbol"]
            inst = session.query(Instrument).filter_by(tradingsymbol=sym).first()
            if inst is None:
                log.warning("%s no instrument for %s — skip leg", _LOG_PREFIX, sym)
                continue
            opt_ltp = ltp_resp.get(f"MCX:{sym}", {}).get("last_price")
            if not opt_ltp or opt_ltp <= 0:
                log.warning("%s LTP missing for %s — skip leg", _LOG_PREFIX, sym)
                continue
            res = compute_delta(inst, float(opt_ltp), F, T)
            if res.delta is None:
                log.warning("%s delta failed for %s: %s", _LOG_PREFIX, sym, res.rejection_reason)
                continue
            pos_delta = p["quantity"] * res.delta * lot_mult
            total_delta += pos_delta
            per_leg.append((sym, inst, p["quantity"], res.delta, pos_delta))

        if not per_leg:
            log.warning("%s no leg deltas computed — skipping", _LOG_PREFIX)
            return

        threshold, _adx_val, _adx_regime = _adx_threshold(
            kite, futures, float(settings.NG_DELTA_HEDGE_THRESHOLD)
        )
        log.info(
            "%s F=%.2f T=%.4fy net δ=%+.0f mmBtu (threshold=±%.0f ADX=%s regime=%s)",
            _LOG_PREFIX, F, T, total_delta, threshold,
            f"{_adx_val:.1f}" if _adx_val is not None else "n/a", _adx_regime,
        )

        # Profit-lock check: if unrealised >= NG_HALF_EXIT_PNL_TRIGGER, halve
        # everything (BUY-to-close on all shorts). Only skip the delta hedge
        # when the half-exit *just fired this tick* (book is about to shrink).
        # A pre-existing flag from a prior tick must not disable hedging.
        if _maybe_half_exit(settings, session, kite, positions):
            return

        # BANKNIFTY stop-loss runs every tick regardless of NG δ. Reads its
        # own positions out of the same kite.positions() list we already have.
        _maybe_bnf_stop_loss(settings, session, kite, positions)

        # Straddle ladder runs after half-exit has fired. One rung per tick.
        _maybe_straddle_ladder(settings, session, kite, positions)

        # Pre-hedge: if δ is already 60%+ of threshold AND order-book imbalance
        # confirms the drift will continue, lower the effective threshold so we
        # act now rather than waiting for a full breach.
        effective_threshold = threshold
        if abs(total_delta) > _PRE_HEDGE_DELTA_PCT * threshold:
            imbalance = _get_futures_imbalance(kite, fut_full)
            if imbalance is not None:
                # Long δ → need to sell CE: bad if bids dominating (price rising)
                # Short δ → need to sell PE: bad if asks dominating (price falling)
                bad_direction = (
                    (total_delta > 0 and imbalance > _PRE_HEDGE_IMBALANCE_THRESH) or
                    (total_delta < 0 and imbalance < -_PRE_HEDGE_IMBALANCE_THRESH)
                )
                if bad_direction:
                    effective_threshold = _PRE_HEDGE_DELTA_PCT * threshold
                    log.info(
                        "%s [PRE-HEDGE] imbalance=%+.3f signals continued drift — "
                        "eff.threshold %.0f→%.0f mmBtu (δ=%+.0f)",
                        _LOG_PREFIX, imbalance, threshold, effective_threshold, total_delta,
                    )
                    _telegram_notify(
                        settings,
                        f"NG δ pre-hedge triggered\n"
                        f"  imbalance: {imbalance:+.3f} ({'bid-heavy↑' if imbalance>0 else 'ask-heavy↓'})\n"
                        f"  net δ: {total_delta:+.0f} mmBtu ({abs(total_delta)/threshold*100:.0f}% of threshold)\n"
                        f"  acting early at eff.threshold={effective_threshold:.0f}",
                    )
                else:
                    log.info(
                        "%s [PRE-HEDGE] imbalance=%+.3f — no directional bias, holding (δ=%+.0f)",
                        _LOG_PREFIX, imbalance, total_delta,
                    )

        if abs(total_delta) <= effective_threshold:
            return

        # Pick deficit side: positive δ → sell CE; negative δ → sell PE
        side = "CE" if total_delta > 0 else "PE"
        candidates = [x for x in per_leg if x[0].endswith(side)]
        if not candidates:
            log.warning("%s no existing %s legs to add to — skipping (|δ|=%.0f)",
                        _LOG_PREFIX, side, abs(total_delta))
            return
        # Nearest-ATM existing leg (smallest |strike − F|)
        candidates.sort(key=lambda x: abs(float(x[1].strike) - F))
        chosen_sym, chosen_inst, _, chosen_delta_unit, _ = candidates[0]
        chosen_strike_dist = abs(float(chosen_inst.strike) - F)

        # ITM tilt: prefer an ITM strike to leave residual delta aligned with trend.
        # PE hedge fires when NG is rising (delta too negative) → ITM PE (strike > F)
        #   → higher δ per lot → 1 lot sell overshoots neutral → residual positive Δ ✓
        # CE hedge fires when NG is falling (delta too positive) → ITM CE (strike < F)
        #   → higher δ per lot → 1 lot sell overshoots neutral → residual negative Δ ✓
        # Only applies when nearest existing leg is OTM; skip if already ITM.
        _hedge_switch_note = ""
        existing_is_itm = (
            (side == "PE" and float(chosen_inst.strike or 0) > F) or
            (side == "CE" and float(chosen_inst.strike or 0) < F)
        )
        _corridor_switched = existing_is_itm  # treat existing ITM as already optimal

        if not existing_is_itm:
            itm_inst = _pick_itm_strike(session, opt_expiry, side, F)
            if itm_inst is not None:
                try:
                    itm_ltp = float(
                        kite.ltp([f"MCX:{itm_inst.tradingsymbol}"])
                        .get(f"MCX:{itm_inst.tradingsymbol}", {})
                        .get("last_price") or 0
                    )
                except Exception as exc:
                    log.warning("%s [ITM-TILT] LTP fetch for %s failed: %s",
                                _LOG_PREFIX, itm_inst.tradingsymbol, exc)
                    itm_ltp = 0.0
                if itm_ltp > 0:
                    itm_res = compute_delta(itm_inst, itm_ltp, F, T)
                    if itm_res.delta is not None:
                        itm_dist = abs(float(itm_inst.strike or 0) - F)
                        log.info(
                            "%s [ITM-TILT] existing %s is OTM → switching to ITM %s "
                            "(strike=%.0f, %.1f pts %s F, δ_unit=%+.4f)",
                            _LOG_PREFIX, chosen_sym, itm_inst.tradingsymbol,
                            float(itm_inst.strike or 0), itm_dist,
                            "above" if side == "PE" else "below", itm_res.delta,
                        )
                        chosen_sym = itm_inst.tradingsymbol
                        chosen_inst = itm_inst
                        chosen_delta_unit = itm_res.delta
                        chosen_strike_dist = itm_dist
                        _corridor_switched = True
                        _hedge_switch_note = (
                            f"\n  [ITM-tilt] {chosen_sym} "
                            f"({'above' if side=='PE' else 'below'} F by {itm_dist:.1f}pts)"
                        )
                    else:
                        log.warning("%s [ITM-TILT] delta failed for %s — using existing %s",
                                    _LOG_PREFIX, itm_inst.tradingsymbol, chosen_sym)
                else:
                    log.warning("%s [ITM-TILT] no LTP for %s — using existing %s",
                                _LOG_PREFIX, itm_inst.tradingsymbol, chosen_sym)
            else:
                log.debug("%s [ITM-TILT] no ITM %s instrument found — using existing %s",
                          _LOG_PREFIX, side, chosen_sym)

        # ATM corridor check: if the nearest existing leg is >_ATM_CORRIDOR_PTS
        # from F, open a fresh ATM position instead of piling onto the OTM leg.
        if not _corridor_switched and chosen_strike_dist > _ATM_CORRIDOR_PTS:
            atm_inst = _pick_atm_strike(session, opt_expiry, side, F)
            if atm_inst is not None and atm_inst.tradingsymbol != chosen_sym:
                atm_dist = abs(float(atm_inst.strike) - F)
                try:
                    atm_ltp_resp = kite.ltp([f"MCX:{atm_inst.tradingsymbol}"])
                    atm_ltp = float(
                        atm_ltp_resp.get(f"MCX:{atm_inst.tradingsymbol}", {}).get("last_price") or 0
                    )
                except Exception as exc:
                    atm_ltp = 0.0
                    log.warning("%s [CORRIDOR] LTP fetch for %s failed: %s",
                                _LOG_PREFIX, atm_inst.tradingsymbol, exc)
                if atm_ltp > 0:
                    atm_res = compute_delta(atm_inst, atm_ltp, F, T)
                    if atm_res.delta is not None:
                        log.info(
                            "%s [CORRIDOR] nearest existing %s is %.1f pts from F=%.2f "
                            "(>%.0f corridor) → switching to fresh ATM %s (%.1f pts away, δ_unit=%+.4f)",
                            _LOG_PREFIX, chosen_sym, chosen_strike_dist, F,
                            _ATM_CORRIDOR_PTS, atm_inst.tradingsymbol, atm_dist, atm_res.delta,
                        )
                        chosen_sym = atm_inst.tradingsymbol
                        chosen_inst = atm_inst
                        chosen_delta_unit = atm_res.delta
                        _corridor_switched = True
                    else:
                        log.warning("%s [CORRIDOR] delta failed for ATM %s — using existing %s",
                                    _LOG_PREFIX, atm_inst.tradingsymbol, chosen_sym)
                else:
                    log.warning("%s [CORRIDOR] no LTP for ATM %s — using existing %s",
                                _LOG_PREFIX, atm_inst.tradingsymbol, chosen_sym)
            else:
                log.debug("%s [CORRIDOR] ATM query returned same strike or None — using %s",
                          _LOG_PREFIX, chosen_sym)
        else:
            log.debug("%s [CORRIDOR] nearest existing %s is %.1f pts from F — within corridor",
                      _LOG_PREFIX, chosen_sym, chosen_strike_dist)

        # Delta-efficiency check: even within the corridor, if the chosen leg's
        # |δ| < _MIN_HEDGE_DELTA it's too OTM — switch to fresh ATM for better
        # delta-per-lot and theta. Skipped if corridor already switched target.
        if not _corridor_switched and abs(chosen_delta_unit) < _MIN_HEDGE_DELTA:
            atm_inst = _pick_atm_strike(session, opt_expiry, side, F)
            if atm_inst is not None and atm_inst.tradingsymbol != chosen_sym:
                try:
                    atm_ltp_resp = kite.ltp([f"MCX:{atm_inst.tradingsymbol}"])
                    atm_ltp = float(
                        atm_ltp_resp.get(f"MCX:{atm_inst.tradingsymbol}", {}).get("last_price") or 0
                    )
                except Exception as exc:
                    atm_ltp = 0.0
                    log.warning("%s [DELTA-EFF] LTP fetch for %s failed: %s",
                                _LOG_PREFIX, atm_inst.tradingsymbol, exc)
                if atm_ltp > 0:
                    atm_res = compute_delta(atm_inst, atm_ltp, F, T)
                    if atm_res.delta is not None:
                        log.info(
                            "%s [DELTA-EFF] existing %s δ=%+.4f < %.2f threshold "
                            "→ switching to fresh ATM %s (δ=%+.4f)",
                            _LOG_PREFIX, chosen_sym, chosen_delta_unit, _MIN_HEDGE_DELTA,
                            atm_inst.tradingsymbol, atm_res.delta,
                        )
                        chosen_sym = atm_inst.tradingsymbol
                        chosen_inst = atm_inst
                        chosen_delta_unit = atm_res.delta
                        _corridor_switched = True  # reuse flag for telegram note
                    else:
                        log.warning("%s [DELTA-EFF] delta failed for ATM %s — using existing %s",
                                    _LOG_PREFIX, atm_inst.tradingsymbol, chosen_sym)
                else:
                    log.warning("%s [DELTA-EFF] no LTP for ATM %s — using existing %s",
                                _LOG_PREFIX, atm_inst.tradingsymbol, chosen_sym)
            else:
                log.debug("%s [DELTA-EFF] ATM same as existing or None — keeping %s",
                          _LOG_PREFIX, chosen_sym)

        # Lot sizing: 1 lot if |δ| ≤ 2×threshold, else 2
        lots = 1 if abs(total_delta) <= 2.0 * threshold else 2
        qty = lots * chosen_inst.lot_size  # MCX option lot_size=1 → qty=lots

        # Projected delta after hedge (SELL adds −1×qty×δ_per_unit×mult)
        added_delta = -qty * chosen_delta_unit * lot_mult
        projected_delta = total_delta + added_delta

        log.info(
            "%s HEDGE: SELL %d lot %s (δ_unit=%+.4f, added=%+.0f) → projected δ=%+.0f",
            _LOG_PREFIX, lots, chosen_sym, chosen_delta_unit, added_delta, projected_delta,
        )

        # Place order
        if settings.DRY_RUN:
            log.info("%s DRY_RUN — skipping order; would SELL %d %s", _LOG_PREFIX, qty, chosen_sym)
            _telegram_notify(
                settings,
                f"NG δ-hedge DRY: would SELL {lots} lot {chosen_sym}\n"
                f"  net δ before: {total_delta:+.0f} mmBtu\n"
                f"  net δ after (est): {projected_delta:+.0f} mmBtu",
            )
            return

        # Primary attempt: SELL the deficit-side option (earns premium).
        action_desc = f"SELL {lots} lot {chosen_sym}"
        oid, mode = _limit_then_market(kite, chosen_inst, "SELL", qty, settings)
        final_inst = chosen_inst
        final_side = "SELL"
        final_qty = qty
        final_added = added_delta

        # Fallback 1a: roll deep-OTM same-type leg → fresh ATM sell.
        # Close 1 lot of the deepest OTM leg (|δ| < 0.25) to free margin,
        # then SELL ATM same-type for credit + better delta efficiency.
        # If ATM sell fails after the close, fall through to Fallback 1b.
        # Skip within 7 days of expiry — ATM theta too low to justify the roll.
        if mode == "INSUFFICIENT_FUNDS" and T > 7.0 / 365:
            deep_otm = sorted(
                [x for x in per_leg if x[0].endswith(side) and abs(x[3]) < 0.25 and x[2] < 0],
                key=lambda x: abs(x[3]),  # furthest OTM first (lowest |delta|)
            )
            if deep_otm:
                otm_sym, otm_inst, _, otm_delta_unit, _ = deep_otm[0]
                log.info(
                    "%s [ROLL] margin blocked; closing deep OTM %s "
                    "(δ_unit=%+.4f, dist=%.1f pts) to free margin",
                    _LOG_PREFIX, otm_sym, otm_delta_unit, abs(float(otm_inst.strike) - F),
                )
                close_oid, close_mode = _limit_then_market(
                    kite, otm_inst, "BUY", otm_inst.lot_size, settings
                )
                if close_oid is not None:
                    # Update Position row for the closed OTM leg
                    otm_pos = (
                        session.query(Position)
                        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
                        .filter(
                            Position.tradingsymbol == otm_sym,
                            Position.exchange == "MCX",
                            ClosedTrade.id == None,  # noqa: E711
                        )
                        .first()
                    )
                    if otm_pos is not None:
                        otm_pos.quantity = otm_pos.quantity + otm_inst.lot_size
                        otm_pos.last_updated_at = now
                        session.commit()
                        log.info("%s [ROLL] OTM Position.id=%d qty→%d",
                                 _LOG_PREFIX, otm_pos.id, otm_pos.quantity)
                    # Now sell ATM same-type
                    atm_inst = _pick_atm_strike(session, opt_expiry, side, F)
                    if atm_inst is not None:
                        try:
                            atm_ltp_val = float(
                                kite.ltp([f"MCX:{atm_inst.tradingsymbol}"])
                                .get(f"MCX:{atm_inst.tradingsymbol}", {})
                                .get("last_price") or 0
                            )
                        except Exception as exc:
                            log.warning("%s [ROLL] LTP for %s failed: %s",
                                        _LOG_PREFIX, atm_inst.tradingsymbol, exc)
                            atm_ltp_val = 0.0
                        atm_delta_unit = None
                        if atm_ltp_val > 0:
                            atm_res = compute_delta(atm_inst, atm_ltp_val, F, T)
                            atm_delta_unit = atm_res.delta
                        roll_qty = lots * atm_inst.lot_size
                        roll_oid, roll_mode = _limit_then_market(
                            kite, atm_inst, "SELL", roll_qty, settings
                        )
                        if roll_oid is not None:
                            roll_added = -roll_qty * (atm_delta_unit or chosen_delta_unit) * lot_mult
                            roll_projected = total_delta + roll_added
                            log.info(
                                "%s [ROLL] success: closed %s + sold %s "
                                "(δ_unit=%+.4f, added=%+.0f, proj δ=%+.0f)",
                                _LOG_PREFIX, otm_sym, atm_inst.tradingsymbol,
                                atm_delta_unit or chosen_delta_unit, roll_added, roll_projected,
                            )
                            oid = roll_oid
                            mode = roll_mode
                            action_desc = f"ROLL {otm_sym}→SELL {lots}lot {atm_inst.tradingsymbol}"
                            final_inst = atm_inst
                            final_side = "SELL"
                            final_qty = roll_qty
                            final_added = roll_added
                            projected_delta = roll_projected
                        else:
                            log.warning(
                                "%s [ROLL] ATM sell %s failed (%s) — falling through to BUY opposite",
                                _LOG_PREFIX, atm_inst.tradingsymbol, roll_mode,
                            )
                    else:
                        log.warning("%s [ROLL] no ATM instrument — falling through to BUY opposite",
                                    _LOG_PREFIX)
                else:
                    log.warning("%s [ROLL] OTM close %s failed (%s) — falling through to BUY opposite",
                                _LOG_PREFIX, otm_sym, close_mode)

        # Fallback 1b: BUY opposite-type option (cheap — only premium, no margin lock).
        # Fires when: no deep-OTM leg exists, roll close failed, or ATM sell failed.
        # Positive δ over threshold → need negative δ → BUY PE.
        # Negative δ over threshold → need positive δ → BUY CE.
        if mode == "INSUFFICIENT_FUNDS":
            opp_type = "PE" if total_delta > 0 else "CE"
            opp_inst = _pick_atm_strike(session, opt_expiry, opp_type, F)
            if opp_inst is not None:
                opp_ltp = ltp_resp.get(f"MCX:{opp_inst.tradingsymbol}", {}).get("last_price")
                if not opp_ltp:
                    try:
                        opp_ltp = float(
                            kite.ltp([f"MCX:{opp_inst.tradingsymbol}"])
                            [f"MCX:{opp_inst.tradingsymbol}"]["last_price"]
                        )
                    except Exception:
                        opp_ltp = None
                opp_delta = None
                if opp_ltp and opp_ltp > 0:
                    opp_res = compute_delta(opp_inst, float(opp_ltp), F, T)
                    opp_delta = opp_res.delta
                if opp_delta is None:
                    log.warning(
                        "%s fallback BUY %s: delta unsolvable — trying futures",
                        _LOG_PREFIX, opp_inst.tradingsymbol,
                    )
                else:
                    # BUY adds +qty × δ_per_unit × mult. δ_PE is negative → reduces +total;
                    # δ_CE is positive → reduces (less-negative) total.
                    opp_qty = lots * opp_inst.lot_size
                    opp_added = opp_qty * opp_delta * lot_mult
                    opp_projected = total_delta + opp_added
                    log.info(
                        "%s margin-blocked SELL → fallback: BUY %d lot %s (δ_unit=%+.4f, added=%+.0f) → projected δ=%+.0f",
                        _LOG_PREFIX, lots, opp_inst.tradingsymbol, opp_delta, opp_added, opp_projected,
                    )
                    oid, mode = _limit_then_market(kite, opp_inst, "BUY", opp_qty, settings)
                    if oid is not None:
                        action_desc = f"BUY {lots} lot {opp_inst.tradingsymbol}"
                        final_inst = opp_inst
                        final_side = "BUY"
                        final_qty = opp_qty
                        final_added = opp_added
                        projected_delta = opp_projected

        # Fallback 2: hedge with front-month NG futures (still needs margin but a
        # single lot may fit if the SELL-option call was blocked due to the
        # 2-lot size, or you've just freed margin elsewhere).
        if oid is None and mode == "INSUFFICIENT_FUNDS":
            fut_inst = (
                session.query(Instrument)
                .filter(
                    Instrument.name == _UNDERLYING,
                    Instrument.instrument_type == "FUT",
                    Instrument.exchange == "MCX",
                    Instrument.expiry != None,  # noqa: E711
                )
                .order_by(Instrument.expiry.asc())
                .first()
            )
            if fut_inst is not None:
                # δ > 0 → SELL FUT (adds -lot_mult per lot); δ < 0 → BUY FUT
                fut_side = "SELL" if total_delta > 0 else "BUY"
                fut_qty = lots * fut_inst.lot_size  # NG futures lot_size=1
                fut_added = (-1 if fut_side == "SELL" else 1) * fut_qty * lot_mult
                fut_projected = total_delta + fut_added
                log.info(
                    "%s margin-blocked options → fallback: %s %d lot %s (added=%+.0f) → projected δ=%+.0f",
                    _LOG_PREFIX, fut_side, lots, fut_inst.tradingsymbol, fut_added, fut_projected,
                )
                oid, mode = _limit_then_market(kite, fut_inst, fut_side, fut_qty, settings)
                if oid is not None:
                    action_desc = f"{fut_side} {lots} lot {fut_inst.tradingsymbol}"
                    final_inst = fut_inst
                    final_side = fut_side
                    final_qty = fut_qty
                    final_added = fut_added
                    projected_delta = fut_projected

        if oid is None:
            log.error(
                "%s all hedge attempts failed (mode=%s, net δ=%+.0f) — no hedge applied",
                _LOG_PREFIX, mode, total_delta,
            )
            _telegram_notify(
                settings,
                f"NG δ-hedge FAILED on all paths (net δ={total_delta:+.0f} mmBtu)\n"
                f"Tried: SELL → BUY opp → futures. All blocked or rejected. "
                f"Please add margin or hedge manually.",
            )
            return

        # Update existing Position row so 23:20 squareoff closes full broker qty.
        pos = (
            session.query(Position)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol == final_inst.tradingsymbol,
                Position.exchange == "MCX",
                ClosedTrade.id == None,  # noqa: E711
            )
            .first()
        )
        if pos is not None:
            old_qty = pos.quantity
            signed_qty = final_qty if final_side == "BUY" else -final_qty
            pos.quantity = old_qty + signed_qty
            pos.last_updated_at = now
            session.commit()
            log.info(
                "%s Position.id=%d qty %d → %d (hedge %s %d)",
                _LOG_PREFIX, pos.id, old_qty, pos.quantity, final_side, final_qty,
            )
        else:
            log.warning(
                "%s no open Position row for %s — squareoff may leave %d %s units uncovered",
                _LOG_PREFIX, final_inst.tradingsymbol, final_qty, final_side,
            )

        _adx_str = f"ADX={_adx_val:.1f} ({_adx_regime})" if _adx_val is not None else f"regime={_adx_regime}"
        _telegram_notify(
            settings,
            f"NG δ-hedge: {action_desc} ({mode}){_hedge_switch_note}\n"
            f"  net δ before: {total_delta:+.0f} mmBtu\n"
            f"  net δ after (est): {projected_delta:+.0f} mmBtu\n"
            f"  threshold: ±{threshold:.0f} mmBtu [{_adx_str}]\n"
            f"  order_id: {oid}",
        )
