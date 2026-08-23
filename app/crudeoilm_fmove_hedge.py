"""CRUDEOILM F-move-triggered delta hedge — sells ATM options.

Different from the band-based `_process_underlying` in `app.delta_hedge`: this
fires on **futures-price movement from the last hedge fill** rather than
|net δ| crossing a band. Every 1 min:

  1. Read CRUDEOILM front-month FUT LTP as F_now.
  2. Compare to F_anchor (persisted per day; set on first tick with open book).
  3. If |F_now - F_anchor| >= CRUDEOILM_FMOVE_HEDGE_TRIGGER_INR (default ₹100):
       a. Compute current portfolio δ across all open CRUDEOILM legs.
       b. If |δ| < MIN_DELTA_UNITS → advance anchor, no order.
       c. If |δ| shrunk vs last-hedge δ → advance anchor, no order (favourable).
       d. Else pick ATM option of the correct type (SELL CE if δ>0, SELL PE if
          δ<0), size lots to zero δ, clamped to MAX_LOTS_PER_TICK.
       e. LIMIT@bid → MARKET after 20s (`_limit_then_market`).
       f. On fill: anchor ← F_now, δ_anchor ← δ_now, hedges_today += 1.

Why SELL options and not FUT:
  User preference — see the /control build conversation. Collects premium,
  uses margin only (no cash out). Trade-off: adds more short γ every hedge,
  so trending days grow the book faster than a FUT hedge would.

Guards: emergency stop, session invalid, MCX-hours-only, freeze in the last
`NG_HEDGE_FREEZE_BEFORE_MIN` before `STRADDLE_SQUAREOFF_TIME`, cooldown min
between hedges, per-day hedge cap.

State: `data/crudeoilm_fmove_hedge_state.json` — resets on date rollover.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime, timedelta
from typing import Any

log = logging.getLogger(__name__)

_LOG_PREFIX = "[crudeoilm_fmove]"
_UNDERLYING = "CRUDEOILM"
_EXCHANGE = "MCX"


def _matches(tradingsymbol: str) -> bool:
    """Prefix = CRUDEOILM AND next char a digit. Same rule as delta_hedge —
    prevents a hypothetical CRUDEOILMX prefix collision."""
    if not tradingsymbol.startswith(_UNDERLYING):
        return False
    rest = tradingsymbol[len(_UNDERLYING):]
    return bool(rest) and rest[0].isdigit()


def _state_path(settings: Any) -> str:
    return getattr(settings, "CRUDEOILM_FMOVE_HEDGE_STATE_PATH",
                   "data/crudeoilm_fmove_hedge_state.json")


def _load_state(settings: Any) -> dict:
    from app.config import IST
    today = datetime.now(IST).date().isoformat()
    blank = {
        "date": today,
        "F_anchor": None,
        "delta_anchor": None,
        "hedges_today": 0,
        "last_hedge_at": None,
    }
    path = _state_path(settings)
    if not os.path.exists(path):
        return blank
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as exc:
        log.warning("%s state unreadable (%s) — starting fresh", _LOG_PREFIX, exc)
        return blank
    if data.get("date") != today:
        log.info("%s state from %s — resetting for %s",
                 _LOG_PREFIX, data.get("date"), today)
        return blank
    return {**blank, **data}


def _save_state(settings: Any, s: dict) -> None:
    path = _state_path(settings)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(s, f, indent=2)
    except Exception as exc:
        log.error("%s failed to save state %s: %s", _LOG_PREFIX, path, exc)


def _telegram(settings: Any, text: str) -> None:
    try:
        from app.commodity_agents.notify import send_telegram
        send_telegram(settings, text)
    except Exception as exc:
        log.warning("%s telegram failed: %s", _LOG_PREFIX, exc)


def _pick_atm(session: Any, expiry: Any, opt_type: str, F: float) -> Any:
    """Nearest-strike CE or PE for the given expiry. None if not in DB."""
    from app.storage import Instrument
    rows = (
        session.query(Instrument)
        .filter(
            Instrument.name == _UNDERLYING,
            Instrument.instrument_type == opt_type,
            Instrument.exchange == _EXCHANGE,
            Instrument.expiry == expiry,
        )
        .all()
    )
    if not rows:
        return None
    return min(rows, key=lambda inst: abs(float(inst.strike or 0) - F))


def run_crudeoilm_fmove_hedge_job(settings: Any, session_factory: Any) -> None:
    """One 1-min wake-up. All exceptions swallowed so the scheduler survives."""
    try:
        _run_impl(settings, session_factory)
    except Exception:
        log.exception("%s tick failed", _LOG_PREFIX)


def _run_impl(settings: Any, session_factory: Any) -> None:
    from app import hedge_tunables, state
    from app.config import IST
    from app.delta_hedge import _limit_then_market
    from app.greeks import compute_delta
    from app.kite_session import get_session_manager
    from app.orders import throttled_quote_call
    from app.storage import ClosedTrade, Instrument, Order, Position

    if not state.is_crudeoilm_fmove_hedge_enabled(
        getattr(settings, "CRUDEOILM_FMOVE_HEDGE_ENABLED", False)
    ):
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
    # Live thresholds: /control override first, .env default behind it. Resolved
    # in one pass per tick so a mid-tick edit cannot split one hedge decision
    # across two generations of the config.
    tun = hedge_tunables.all_effective(settings, "crudeoilm_fmove")

    opts = [
        p for p in positions
        if p.get("exchange") == _EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches(p.get("tradingsymbol") or "")
        and (p["tradingsymbol"].endswith("CE") or p["tradingsymbol"].endswith("PE"))
    ]
    futs = [
        p for p in positions
        if p.get("exchange") == _EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches(p.get("tradingsymbol") or "")
        and p["tradingsymbol"].endswith("FUT")
    ]
    if not opts:
        log.debug("%s no CRUDEOILM options open — nothing to hedge", _LOG_PREFIX)
        return

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
            log.warning("%s no front-month FUT instrument — skipping", _LOG_PREFIX)
            return
        fut_full = f"{_EXCHANGE}:{fut_inst.tradingsymbol}"

        # Batch: FUT + all option legs in one quote call
        leg_fulls = [f"{_EXCHANGE}:{p['tradingsymbol']}" for p in opts]
        try:
            ltp_resp = throttled_quote_call(kite.ltp, [fut_full] + leg_fulls)
        except Exception as exc:
            log.warning("%s ltp batch failed: %s", _LOG_PREFIX, exc)
            return
        F_now = float(ltp_resp.get(fut_full, {}).get("last_price") or 0)
        if F_now <= 0:
            log.warning("%s F invalid (%s) — skipping", _LOG_PREFIX, F_now)
            return

        hedge_state = _load_state(settings)
        F_anchor = hedge_state.get("F_anchor")

        # First-tick anchor: no baseline yet, so we can't judge a move.
        if F_anchor is None:
            hedge_state["F_anchor"] = F_now
            _save_state(settings, hedge_state)
            log.info("%s anchored at F=%.2f (first tick with open book today)",
                     _LOG_PREFIX, F_now)
            _telegram(
                settings,
                f"<b>[CRUDEOILM fmove-hedge]</b> anchored at F=₹{F_now:,.0f}\n"
                f"Trigger: any ±₹{tun['trigger_inr']:.0f} "
                f"move from here, hedge with SELL ATM option.",
            )
            return

        # Freeze near the 23:20 squareoff — hedges placed at 23:15 pay spread
        # for delta that gets flattened minutes later.
        freeze_min = int(getattr(settings, "NG_HEDGE_FREEZE_BEFORE_MIN", 20))
        if freeze_min > 0 and settings.STRADDLE_SQUAREOFF_TIME:
            try:
                sq_h, sq_m = (int(x) for x in settings.STRADDLE_SQUAREOFF_TIME.split(":"))
                freeze_from = (
                    datetime.combine(now.date(), dtime(sq_h, sq_m), tzinfo=IST)
                    - timedelta(minutes=freeze_min)
                )
                if now >= freeze_from:
                    log.info("%s [FREEZE] within %d min of %s — holding (F=%.2f, anchor=%.2f)",
                             _LOG_PREFIX, freeze_min, settings.STRADDLE_SQUAREOFF_TIME,
                             F_now, F_anchor)
                    return
            except ValueError:
                log.warning("%s [FREEZE] bad STRADDLE_SQUAREOFF_TIME=%r",
                            _LOG_PREFIX, settings.STRADDLE_SQUAREOFF_TIME)

        # Per-day cap
        daily_cap = int(tun["max_hedges_per_day"])
        if daily_cap > 0 and int(hedge_state.get("hedges_today", 0)) >= daily_cap:
            log.info("%s daily cap %d reached — holding", _LOG_PREFIX, daily_cap)
            return

        # Cooldown
        cooldown_min = int(tun["cooldown_min"])
        last_hedge_at = hedge_state.get("last_hedge_at")
        if cooldown_min > 0 and last_hedge_at:
            try:
                since = (now - datetime.fromisoformat(last_hedge_at)).total_seconds() / 60.0
                if since < cooldown_min:
                    log.info("%s [COOLDOWN] last hedge %.1f min ago (< %d) — holding",
                             _LOG_PREFIX, since, cooldown_min)
                    return
            except (ValueError, TypeError):
                log.warning("%s [COOLDOWN] unparseable last_hedge_at=%r",
                            _LOG_PREFIX, last_hedge_at)

        # Trigger: has F moved trigger threshold from anchor?
        move = F_now - F_anchor
        trigger = float(tun["trigger_inr"])
        log.info("%s F=%.2f anchor=%.2f move=%+.2f (trigger ±%.0f)",
                 _LOG_PREFIX, F_now, F_anchor, move, trigger)
        if abs(move) < trigger:
            return

        # ── Compute portfolio δ ─────────────────────────────────────────────
        lot_units = settings.MCX_LOT_UNITS.get(_UNDERLYING, 10)
        close_h, close_m = (int(x) for x in settings.SESSION_CLOSE_MCX.split(":"))

        # Sample expiry from first open leg (assumed uniform — matches how the
        # scheduled straddle books legs)
        first_inst = session.query(Instrument).filter_by(
            tradingsymbol=opts[0]["tradingsymbol"]
        ).first()
        if first_inst is None or first_inst.expiry is None:
            log.warning("%s no instrument row for %s — skipping",
                        _LOG_PREFIX, opts[0]["tradingsymbol"])
            return
        opt_expiry = first_inst.expiry
        expiry_dt = datetime.combine(opt_expiry, dtime(close_h, close_m), tzinfo=IST)
        T = (expiry_dt - now).total_seconds() / 31_557_600.0
        if T <= 1.0 / 365:
            log.info("%s T=%.4fy too small — skipping near expiry", _LOG_PREFIX, T)
            return

        total_delta = 0.0
        # Futures legs contribute δ=+1/unit but are never hedged into.
        for fp in futs:
            fd = int(fp["quantity"]) * 1.0 * lot_units
            total_delta += fd
            log.info("%s FUT leg %s qty=%+d δ=%+.1f",
                     _LOG_PREFIX, fp["tradingsymbol"], fp["quantity"], fd)

        per_leg_ok = False
        for p in opts:
            sym = p["tradingsymbol"]
            inst = session.query(Instrument).filter_by(tradingsymbol=sym).first()
            if inst is None:
                log.warning("%s no instrument row for %s — skip leg", _LOG_PREFIX, sym)
                continue
            ltp = float(ltp_resp.get(f"{_EXCHANGE}:{sym}", {}).get("last_price") or 0)
            if ltp <= 0:
                log.warning("%s LTP missing for %s — skip leg", _LOG_PREFIX, sym)
                continue
            res = compute_delta(inst, ltp, F_now, T)
            if res.delta is None:
                # Deep-ITM near expiry fallback (rare on CRUDEOILM but cheap to guard).
                strike = float(inst.strike or 0)
                if inst.instrument_type == "CE" and F_now > strike:
                    d_unit = 0.95
                elif inst.instrument_type == "PE" and F_now < strike:
                    d_unit = -0.95
                else:
                    log.warning("%s δ failed for %s (%s) — skip", _LOG_PREFIX, sym,
                                res.rejection_reason)
                    continue
                log.warning("%s δ solver failed for %s (deep ITM) — using approx %+.2f",
                            _LOG_PREFIX, sym, d_unit)
            else:
                d_unit = res.delta
            leg_d = int(p["quantity"]) * d_unit * lot_units
            total_delta += leg_d
            per_leg_ok = True
            log.info("%s %s %s qty=%+d @ %.2f δ_unit=%+.4f → δ=%+.1f",
                     _LOG_PREFIX, inst.instrument_type, sym, p["quantity"],
                     ltp, d_unit, leg_d)

        if not per_leg_ok:
            log.warning("%s no leg deltas computed — skipping", _LOG_PREFIX)
            return

        log.info("%s F move ±%.0f BREACHED (%+.2f from %.2f) | net δ=%+.2f",
                 _LOG_PREFIX, trigger, move, F_anchor, total_delta)

        # Near-neutral book — nothing worth hedging. Advance anchor so we
        # don't log the same "over trigger" line every minute.
        min_delta = float(tun["min_delta_units"])
        if abs(total_delta) < min_delta:
            log.info("%s [SKIP] |δ|=%.1f < %.1f min — advancing anchor to F=%.2f",
                     _LOG_PREFIX, abs(total_delta), min_delta, F_now)
            hedge_state["F_anchor"] = F_now
            hedge_state["delta_anchor"] = total_delta
            _save_state(settings, hedge_state)
            return

        # Direction gate: user wants "only when δ drifting AWAY from zero".
        # Compare |δ_now| to |δ_at_last_hedge| (or δ_anchor if no hedge yet).
        delta_prev = hedge_state.get("delta_anchor")
        if delta_prev is None:
            # First evaluation this session — treat as growing so we take the hedge.
            delta_prev = 0.0
        growing = abs(total_delta) > abs(delta_prev)
        if not growing:
            log.info("%s [SKIP] |δ|=%.1f <= |δ_prev|=%.1f (reverting) — "
                     "advancing anchor without ordering",
                     _LOG_PREFIX, abs(total_delta), abs(delta_prev))
            hedge_state["F_anchor"] = F_now
            hedge_state["delta_anchor"] = total_delta
            _save_state(settings, hedge_state)
            return

        # ── Pick option to SELL ─────────────────────────────────────────────
        # δ > 0 → need −δ → SELL CE (short CE = −δ)
        # δ < 0 → need +δ → SELL PE (short PE = +δ, since PE has negative δ)
        opt_type = "CE" if total_delta > 0 else "PE"
        atm_inst = _pick_atm(session, opt_expiry, opt_type, F_now)
        if atm_inst is None:
            log.warning("%s no ATM %s instrument in DB — skipping", _LOG_PREFIX, opt_type)
            return
        atm_full = f"{_EXCHANGE}:{atm_inst.tradingsymbol}"
        try:
            atm_ltp = float(kite.ltp([atm_full]).get(atm_full, {}).get("last_price") or 0)
        except Exception as exc:
            log.warning("%s ATM ltp failed: %s", _LOG_PREFIX, exc)
            return
        if atm_ltp <= 0:
            log.warning("%s ATM LTP zero for %s — skipping",
                        _LOG_PREFIX, atm_inst.tradingsymbol)
            return
        atm_res = compute_delta(atm_inst, atm_ltp, F_now, T)
        if atm_res.delta is None or abs(atm_res.delta) < 0.05:
            log.warning("%s ATM δ unusable for %s (%s) — skipping",
                        _LOG_PREFIX, atm_inst.tradingsymbol,
                        atm_res.rejection_reason or f"δ={atm_res.delta}")
            return
        atm_delta_unit = atm_res.delta

        # ── Solve lots ──────────────────────────────────────────────────────
        # SELL 1 lot adds −sign(atm_δ) × |atm_δ| × lot_units to net δ.
        # For CE (atm_δ > 0): adds −|atm_δ|×lot_units (negative). Right for δ>0.
        # For PE (atm_δ < 0): adds +|atm_δ|×lot_units (positive). Right for δ<0.
        # Lots to zero out δ: |δ| / (|atm_δ|×lot_units), rounded.
        max_lots = int(tun["max_lots_per_tick"])
        raw = abs(total_delta) / (abs(atm_delta_unit) * lot_units)
        lots = max(1, round(raw))
        lots = min(lots, max_lots)

        added_delta = -lots * atm_delta_unit * lot_units  # SELL sign flip
        projected = total_delta + added_delta

        log.info(
            "%s HEDGE PLAN: SELL %d lot %s (atm_δ=%+.4f, adds=%+.1f) → proj δ=%+.1f "
            "(cap=%d, raw=%.2f)",
            _LOG_PREFIX, lots, atm_inst.tradingsymbol, atm_delta_unit,
            added_delta, projected, max_lots, raw,
        )

        if settings.DRY_RUN:
            log.info("%s DRY_RUN — order not placed", _LOG_PREFIX)
            _telegram(
                settings,
                f"<b>[CRUDEOILM fmove-hedge · DRY]</b> would SELL {lots} lot "
                f"{atm_inst.tradingsymbol}\n"
                f"F: {F_anchor:.0f} → {F_now:.0f} ({move:+.0f})\n"
                f"δ before: {total_delta:+.1f} → est after: {projected:+.1f}",
            )
            return

        qty = lots * atm_inst.lot_size  # MCX option lot_size=1, so qty=lots
        oid, mode = _limit_then_market(kite, atm_inst, "SELL", qty, settings)
        if oid is None:
            log.error("%s hedge failed (mode=%s) — anchor NOT advanced",
                      _LOG_PREFIX, mode)
            _telegram(
                settings,
                f"<b>[CRUDEOILM fmove-hedge · FAILED]</b>\n"
                f"F: {F_anchor:.0f} → {F_now:.0f} ({move:+.0f})\n"
                f"δ: {total_delta:+.1f} | mode: {mode}\n"
                f"Wanted: SELL {lots} lot {atm_inst.tradingsymbol}. Manual attention.",
            )
            return

        # Mirror to Position table so EOD squareoff and portfolio-δ views
        # see the new short. If no Position row exists (fresh strike), squareoff
        # still closes via kite.positions().
        pos = (
            session.query(Position)
            .join(Order, Position.order_id == Order.id)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol == atm_inst.tradingsymbol,
                Position.exchange == _EXCHANGE,
                ClosedTrade.id == None,  # noqa: E711
                Order.dry_run == False,  # noqa: E712
            )
            .first()
        )
        if pos is not None:
            old_qty = pos.quantity
            pos.quantity = old_qty - qty  # SELL reduces (further negative)
            pos.last_updated_at = now
            session.commit()
            log.info("%s Position.id=%d qty %d→%d (SELL %d)",
                     _LOG_PREFIX, pos.id, old_qty, pos.quantity, qty)
        else:
            log.warning(
                "%s no open Position row for %s — squareoff will still close it "
                "via kite.positions",
                _LOG_PREFIX, atm_inst.tradingsymbol,
            )

        # Advance state on successful fill
        hedge_state["F_anchor"] = F_now
        hedge_state["delta_anchor"] = projected  # what we expect δ to be after the fill
        hedge_state["hedges_today"] = int(hedge_state.get("hedges_today", 0)) + 1
        hedge_state["last_hedge_at"] = now.isoformat()
        _save_state(settings, hedge_state)

        _telegram(
            settings,
            f"<b>[CRUDEOILM fmove-hedge]</b> SELL {lots} lot "
            f"{atm_inst.tradingsymbol} ({mode})\n"
            f"F: {F_anchor:.0f} → {F_now:.0f} ({move:+.0f})\n"
            f"δ before: {total_delta:+.1f} → est after: {projected:+.1f}\n"
            f"hedge #{hedge_state['hedges_today']} today | oid: {oid}\n"
            f"new anchor: {F_now:.0f}",
        )
