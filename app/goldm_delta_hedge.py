"""GOLDM delta-hedge cron — MCX hours, 5-min cadence, price-movement triggered.

Different design from NG/CRUDEOILM/BNF hedges: this fires on **futures-price
movement** rather than net-delta magnitude. Every 5 min:

  1. Read GOLDM front-month FUT LTP as F_now
  2. Compare to F_at_last_checkpoint (persisted per day)
  3. If |F_now - F_checkpoint| >= GOLDM_HEDGE_TRIGGER_INR (default ₹300):
       a. Compute current portfolio δ across all open GOLDM legs (options + FUT)
       b. Solve hedge lots to bring |δ| to minimum: lots = round(|δ| / 10)
          (each FUT lot = 1 δ/unit × 10 units/lot = 10 δ units)
       c. Direction: δ > 0 → SELL FUT; δ < 0 → BUY FUT
       d. Skip if lots = 0 (|δ| < 5) — would overshoot with fractional lot
       e. Place LIMIT@bid → escalate to MARKET after 20s
       f. Update checkpoint to F_now on successful fill
  4. Telegram-notify the action

Why FUT for the hedge (not options):
  - Each FUT lot moves δ by exactly ±10 units (no gamma/theta/vega)
  - Cheapest correction (~₹50-100 spread only vs ~₹500-1500 theta bleed)
  - Options-vs-options rebalancing adds MORE short γ, which is what we're
    trying to defend against on expiry day

State: data/goldm_hedge_state.json — {date, F_checkpoint, hedges_today,
last_hedge_at}. Resets on date rollover. Checkpoint gets set on first tick
that sees an open GOLDM book, then updates only after successful hedges.
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

_LOG_PREFIX = "[goldm_hedge]"
_UNDERLYING = "GOLDM"
_EXCHANGE = "MCX"


def _matches_goldm(tradingsymbol: str) -> bool:
    """Prefix must be GOLDM and next char a digit (expiry). Prevents matching
    hypothetical GOLDM-adjacent symbols and avoids partial-name collisions."""
    if not tradingsymbol.startswith(_UNDERLYING):
        return False
    rest = tradingsymbol[len(_UNDERLYING):]
    return bool(rest) and rest[0].isdigit()


def _load_state(settings: Any) -> dict:
    from app.config import IST as _IST
    today = datetime.now(_IST).date().isoformat()
    path = getattr(settings, "GOLDM_HEDGE_STATE_PATH", "")
    blank = {"date": today, "F_checkpoint": None, "hedges_today": 0, "last_hedge_at": None}
    if not path or not os.path.exists(path):
        return blank
    try:
        with open(path) as f:
            state = json.load(f)
    except Exception as exc:
        log.warning("%s state unreadable (%s) — starting fresh", _LOG_PREFIX, exc)
        return blank
    if state.get("date") != today:
        log.info("%s state from %s — resetting for %s",
                 _LOG_PREFIX, state.get("date"), today)
        return blank
    return {**blank, **state}


def _save_state(settings: Any, state: dict) -> None:
    path = getattr(settings, "GOLDM_HEDGE_STATE_PATH", "")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        log.error("%s failed to save state %s: %s", _LOG_PREFIX, path, exc)


def _telegram(settings: Any, text: str) -> None:
    try:
        from app.commodity_agents.notify import send_telegram
        send_telegram(settings, text)
    except Exception as exc:
        log.warning("%s telegram send failed: %s", _LOG_PREFIX, exc)


def run_goldm_hedge_job(settings: Any, session_factory: Any) -> None:
    """One wake-up. All exceptions swallowed — must never crash scheduler."""
    try:
        _run_impl(settings, session_factory)
    except Exception:
        log.exception("%s tick failed", _LOG_PREFIX)


def _run_impl(settings: Any, session_factory: Any) -> None:
    from app import hedge_tunables, state
    from app.config import IST
    from app.greeks import compute_delta
    from app.kite_session import get_session_manager
    from app.orders import throttled_quote_call
    from app.storage import ClosedTrade, Instrument, Order, Position

    if not getattr(settings, "GOLDM_HEDGE_ENABLED", False):
        return
    if state.is_emergency_stop():
        log.debug("%s emergency stop — skipping", _LOG_PREFIX)
        return
    if state.get_session_invalid():
        log.debug("%s session invalid — skipping", _LOG_PREFIX)
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.warning("%s kite unavailable: %s", _LOG_PREFIX, exc)
        return

    try:
        positions = kite.positions().get("net", []) or []
    except Exception as exc:
        log.warning("%s kite.positions() failed: %s", _LOG_PREFIX, exc)
        return

    now = datetime.now(IST)
    # Live thresholds: /control override first, .env default behind it. One pass
    # per tick so a mid-tick edit cannot split a single hedge decision across two
    # generations of the config.
    tun = hedge_tunables.all_effective(settings, "goldm")

    goldm_opts = [
        p for p in positions
        if p.get("exchange") == _EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches_goldm(p.get("tradingsymbol") or "")
        and (p["tradingsymbol"].endswith("CE") or p["tradingsymbol"].endswith("PE"))
    ]
    goldm_futs = [
        p for p in positions
        if p.get("exchange") == _EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches_goldm(p.get("tradingsymbol") or "")
        and p["tradingsymbol"].endswith("FUT")
    ]

    if not goldm_opts and not goldm_futs:
        log.debug("%s no open GOLDM legs — nothing to hedge", _LOG_PREFIX)
        return

    # Front-month GOLDM futures — used both as F for delta AND as the hedge
    # instrument. If it can't be resolved, we can't do either job.
    with session_factory() as session:
        fut_inst = (
            session.query(Instrument)
            .filter(
                Instrument.name == _UNDERLYING,
                Instrument.instrument_type == "FUT",
                Instrument.exchange == _EXCHANGE,
                Instrument.expiry != None,  # noqa: E711
            )
            .order_by(Instrument.expiry.asc())
            .first()
        )
        if fut_inst is None:
            log.warning("%s no front-month futures instrument — skipping", _LOG_PREFIX)
            return
        fut_full = f"{_EXCHANGE}:{fut_inst.tradingsymbol}"

        try:
            F_now = float(throttled_quote_call(kite.ltp, [fut_full])[fut_full]["last_price"])
        except Exception as exc:
            log.warning("%s F fetch failed: %s", _LOG_PREFIX, exc)
            return
        if F_now <= 0:
            log.warning("%s F invalid (%s) — skipping", _LOG_PREFIX, F_now)
            return

        hedge_state = _load_state(settings)
        F_checkpoint = hedge_state.get("F_checkpoint")

        # First tick with an open book (or after date rollover) → set checkpoint,
        # log the anchor, do NOT hedge (no baseline to compare against yet).
        if F_checkpoint is None:
            hedge_state["F_checkpoint"] = F_now
            _save_state(settings, hedge_state)
            log.info("%s checkpoint anchored at F=%.2f (first tick with open book today)",
                     _LOG_PREFIX, F_now)
            _telegram(
                settings,
                f"<b>[GOLDM hedge]</b> anchored at F=₹{F_now:,.0f}\n"
                f"Trigger: any ₹{tun['trigger_inr']:.0f} move from here",
            )
            return

        # Freeze near MCX close — a hedge placed at 23:15 gets marked to
        # settlement 15 min later, paying spread for nothing.
        freeze_min = int(tun["freeze_before_min"])
        if freeze_min > 0:
            close_h, close_m = (int(x) for x in settings.SESSION_CLOSE_MCX.split(":"))
            freeze_from = (
                datetime.combine(now.date(), dtime(close_h, close_m), tzinfo=IST)
                - timedelta(minutes=freeze_min)
            )
            if now >= freeze_from:
                log.info(
                    "%s [FREEZE] within %d min of %s close — holding (F=%.2f, checkpoint=%.2f)",
                    _LOG_PREFIX, freeze_min, settings.SESSION_CLOSE_MCX, F_now, F_checkpoint,
                )
                return

        # Daily hedge cap
        daily_cap = int(tun["max_hedges_per_day"])
        if daily_cap > 0 and int(hedge_state.get("hedges_today", 0)) >= daily_cap:
            log.info("%s daily cap %d reached — holding", _LOG_PREFIX, daily_cap)
            return

        # Movement check
        move = F_now - F_checkpoint
        trigger = float(tun["trigger_inr"])
        log.info(
            "%s F=%.2f checkpoint=%.2f move=%+.2f (trigger ±%.0f)",
            _LOG_PREFIX, F_now, F_checkpoint, move, trigger,
        )
        if abs(move) < trigger:
            return

        # ── Compute current portfolio δ ─────────────────────────────────────
        lot_units = settings.MCX_LOT_UNITS.get(_UNDERLYING, 10)
        close_h, close_m = (int(x) for x in settings.SESSION_CLOSE_MCX.split(":"))

        # Batch LTP for all option legs (needed for compute_delta)
        leg_fulls = [f"{_EXCHANGE}:{p['tradingsymbol']}" for p in goldm_opts]
        try:
            ltp_resp = throttled_quote_call(kite.ltp, leg_fulls) if leg_fulls else {}
        except Exception as exc:
            log.warning("%s ltp batch failed: %s", _LOG_PREFIX, exc)
            return

        total_delta = 0.0
        # Futures legs: δ = 1 per unit
        for fp in goldm_futs:
            fd = int(fp["quantity"]) * 1.0 * lot_units
            total_delta += fd
            log.info("%s FUT leg %s qty=%+d δ=%+.1f",
                     _LOG_PREFIX, fp["tradingsymbol"], fp["quantity"], fd)

        for p in goldm_opts:
            sym = p["tradingsymbol"]
            inst = session.query(Instrument).filter_by(tradingsymbol=sym).first()
            if inst is None:
                log.warning("%s no instrument row for %s — skip leg", _LOG_PREFIX, sym)
                continue
            ltp = ltp_resp.get(f"{_EXCHANGE}:{sym}", {}).get("last_price") or 0
            if ltp <= 0:
                log.warning("%s LTP missing for %s — skip leg", _LOG_PREFIX, sym)
                continue
            expiry_dt = datetime.combine(inst.expiry, dtime(close_h, close_m), tzinfo=IST)
            T = (expiry_dt - now).total_seconds() / 31_557_600.0
            if T <= 0:
                log.warning("%s %s already expired — skip leg", _LOG_PREFIX, sym)
                continue
            res = compute_delta(inst, float(ltp), F_now, T)
            if res.delta is None:
                # Deep-ITM near expiry: BS IV solver fails. Sign-only fallback.
                strike = float(inst.strike or 0)
                if inst.instrument_type == "CE" and F_now > strike:
                    approx = 0.95
                elif inst.instrument_type == "PE" and F_now < strike:
                    approx = -0.95
                else:
                    log.warning("%s δ failed for %s and not deep-ITM — skip", _LOG_PREFIX, sym)
                    continue
                log.warning("%s δ solver failed for %s (deep ITM) — using approx %+.2f",
                            _LOG_PREFIX, sym, approx)
                d_unit = approx
            else:
                d_unit = res.delta
            leg_d = int(p["quantity"]) * d_unit * lot_units
            total_delta += leg_d
            log.info("%s %s %s qty=%+d @ %.2f δ_unit=%+.4f → δ=%+.1f",
                     _LOG_PREFIX, inst.instrument_type, sym, p["quantity"], ltp, d_unit, leg_d)

        log.info(
            "%s F move ±%.0f BREACHED (%+.0f from %.2f) | net δ=%+.2f",
            _LOG_PREFIX, trigger, move, F_checkpoint, total_delta,
        )

        # ── Solve hedge lots ─────────────────────────────────────────────────
        # Each FUT lot = 1 δ/unit × lot_units = 10 δ units for GOLDM.
        # Target: |δ_after| minimized. lots = round(|δ| / 10), direction opposite.
        min_delta = float(tun["min_delta_units"])
        if abs(total_delta) < min_delta:
            log.info(
                "%s [SKIP] |δ|=%.1f < %.1f threshold — F moved but book is already "
                "near-neutral. Advancing checkpoint to F=%.2f without ordering.",
                _LOG_PREFIX, abs(total_delta), min_delta, F_now,
            )
            hedge_state["F_checkpoint"] = F_now
            _save_state(settings, hedge_state)
            return

        lots = round(abs(total_delta) / lot_units)
        if lots <= 0:
            log.info("%s [SKIP] solved 0 lots — advancing checkpoint", _LOG_PREFIX)
            hedge_state["F_checkpoint"] = F_now
            _save_state(settings, hedge_state)
            return
        max_lots = int(tun["max_lots_per_tick"])
        lots = min(lots, max_lots)

        side = "SELL" if total_delta > 0 else "BUY"
        expected_added = (-lots if side == "SELL" else lots) * lot_units  # in δ units
        expected_after = total_delta + expected_added

        log.info(
            "%s HEDGE PLAN: %s %d lot %s (adds %+d δ → proj δ=%+.1f)",
            _LOG_PREFIX, side, lots, fut_inst.tradingsymbol, expected_added, expected_after,
        )

        if settings.DRY_RUN:
            log.info("%s DRY_RUN — order not placed", _LOG_PREFIX)
            _telegram(
                settings,
                f"<b>[GOLDM hedge · DRY]</b> would {side} {lots} lot {fut_inst.tradingsymbol}\n"
                f"F moved: {F_checkpoint:.0f} → {F_now:.0f} ({move:+.0f})\n"
                f"net δ before: {total_delta:+.1f}\n"
                f"est δ after: {expected_after:+.1f}",
            )
            return

        from app.delta_hedge import _limit_then_market
        oid, mode = _limit_then_market(kite, fut_inst, side, lots, settings)

        if oid is None:
            log.error("%s hedge failed (mode=%s) — checkpoint NOT advanced", _LOG_PREFIX, mode)
            _telegram(
                settings,
                f"<b>[GOLDM hedge · FAILED]</b>\n"
                f"F moved: {F_checkpoint:.0f} → {F_now:.0f} ({move:+.0f})\n"
                f"net δ: {total_delta:+.1f} | mode: {mode}\n"
                f"Wanted: {side} {lots} lot {fut_inst.tradingsymbol}. Manual attention.",
            )
            return

        # Bookkeeping: update Position row for the FUT if it exists so EOD
        # squareoff closes full broker qty. If not, this is a new FUT position
        # that squareoff (which reads from kite.positions) still handles fine.
        pos = (
            session.query(Position)
            .join(Order, Position.order_id == Order.id)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol == fut_inst.tradingsymbol,
                Position.exchange == _EXCHANGE,
                ClosedTrade.id == None,  # noqa: E711
                Order.dry_run == False,  # noqa: E712
            )
            .first()
        )
        if pos is not None:
            old_qty = pos.quantity
            signed_qty = lots if side == "BUY" else -lots
            pos.quantity = old_qty + signed_qty
            pos.last_updated_at = now
            session.commit()
            log.info(
                "%s Position.id=%d qty %d→%d (hedge %s %d)",
                _LOG_PREFIX, pos.id, old_qty, pos.quantity, side, lots,
            )
        else:
            log.warning(
                "%s no open Position row for %s — squareoff will still close it via kite.positions",
                _LOG_PREFIX, fut_inst.tradingsymbol,
            )

        # Advance checkpoint to current F, log the fire, save state
        hedge_state["F_checkpoint"] = F_now
        hedge_state["hedges_today"] = int(hedge_state.get("hedges_today", 0)) + 1
        hedge_state["last_hedge_at"] = now.isoformat()
        _save_state(settings, hedge_state)

        _telegram(
            settings,
            f"<b>[GOLDM hedge]</b> {side} {lots} lot {fut_inst.tradingsymbol} ({mode})\n"
            f"F moved: {F_checkpoint:.0f} → {F_now:.0f} ({move:+.0f})\n"
            f"net δ before: {total_delta:+.1f}\n"
            f"net δ after (est): {expected_after:+.1f}\n"
            f"hedge #{hedge_state['hedges_today']} today | order_id: {oid}\n"
            f"new checkpoint: {F_now:.0f}",
        )
