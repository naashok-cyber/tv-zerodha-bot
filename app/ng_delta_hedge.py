"""NATURALGAS delta hedge — spec + the NG-gated one-shot helpers.

The band engine itself lives in `app/hedge_core.py`; this module owns only what
is genuinely NG-specific:

  * `build_spec()` — cadence, contract units and band bounds in mmBtu, with
    every knob resolved through `app.hedge_tunables` so a /control edit applies
    from the next rebalance without a rebuild.
  * the three one-shot helpers that ride the same tick — half-exit, the
    BANKNIFTY stop-loss and the straddle ladder.

The BANKNIFTY stop-loss is historically gated on the NG toggle and scans the
same `positions()` payload, so it stays here rather than moving to
`app/bnf_delta_hedge.py` — moving it would silently change both its gate and its
cadence.

Cadence: interval 2, phase 1 -> :01 :03 :05 ... :59 (odd minutes avoid :00, the
most contended slot).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from app.hedge_core import (
    HedgeSpec,
    _limit_then_market,
    _telegram_notify,
)

log = logging.getLogger(__name__)

_LOG_PREFIX = "[ng_hedge]"
_HALFEXIT_PREFIX = "[ng_halfexit]"
_LADDER_PREFIX = "[ng_ladder]"
_BNF_SL_PREFIX = "[bnf_sl]"
_UNDERLYING = "NATURALGAS"
_BNF_UNDERLYING = "BANKNIFTY"

SLUG = "ng"   # app/hedge_tunables.py registry key


def build_spec(settings: Any) -> HedgeSpec:
    """NG's spec for this tick, with live thresholds folded in.

    Resolved per wake-up, never cached: that is what makes a /control change
    take effect on the next rebalance.
    """
    from app import hedge_tunables

    t = hedge_tunables.all_effective(settings, SLUG)
    return HedgeSpec(
        name=_UNDERLYING,
        slug=SLUG,
        exchange="MCX",
        lot_units=settings.MCX_LOT_UNITS["NATURALGAS"],
        band_base=float(settings.NG_HEDGE_BAND_BASE),
        band_notional_inr=float(t["band_notional_inr"]),
        vol_factor=float(t["vol_factor"]),
        band_min=float(t["band_min"]),
        band_max=float(t["band_max"]),
        cooldown_min=int(t["cooldown_min"]),
        max_lots_per_tick=int(t["max_lots_per_tick"]),
        ref_lots=int(settings.NG_HEDGE_BAND_REF_LOTS),
        interval_min=int(settings.NG_HEDGE_INTERVAL_MIN),
        phase_min=int(settings.NG_HEDGE_PHASE_MIN),
        velocity_samples=int(settings.NG_HEDGE_VELOCITY_SAMPLES),
        adx_fallback=float(settings.NG_DELTA_HEDGE_THRESHOLD),
    )


def run_one_shots(settings: Any, session: Any, kite: Any, positions: list) -> bool:
    """Half-exit, BNF stop-loss and straddle ladder, in that order.

    Returns True when the half-exit fired, which ends the whole tick — it has
    just restructured the book, so any band computed from the pre-exit positions
    payload is stale.
    """
    if _maybe_half_exit(settings, session, kite, positions):
        return True
    _maybe_bnf_stop_loss(settings, session, kite, positions)
    _maybe_straddle_ladder(settings, session, kite, positions)
    return False


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

