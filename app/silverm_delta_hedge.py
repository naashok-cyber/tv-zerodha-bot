"""SILVERM F-move-triggered delta hedge — BUYS OTM option wings.

Different from CRUDEOILM/NG which SELL more options when book δ drifts. Silver's
₹/kg is 20-100× crude/gas, so notional-parity bands collapse below one ATM
lot's granularity (CLAUDE.md granularity rule) — a FUT hedge would ping-pong.
This hedger BUYS OTM wings sized to bring the book to |δ| ≤ MAX_RESIDUAL_KG.

Why BUY (not SELL) options here:
  1. Book is already short-vol / short-γ (short straddle + strangle). Selling
     more just deepens the same exposure.
  2. Long OTM options give ~1-1.5 kg δ/lot vs 5 kg/lot for FUT — 3× finer
     granularity, so one wing can zero the book on a ±₹1500 move.
  3. Adds long γ at the tail — protects against a runaway silver move.
  4. Bounded loss (premium only), no margin drain.

Every 1 min (Mon–Fri 09–23 IST):
  1. Read SILVERM front-month FUT LTP as F_now.
  2. Compare to F_anchor (persisted per day; set on first tick with open book).
  3. If |F_now - F_anchor| >= SILVERM_HEDGE_TRIGGER_INR (default ₹1500):
       a. Compute current book δ across all open SILVERM option legs.
       b. If |δ_kg| ≤ MAX_RESIDUAL_KG → advance anchor, no order.
       c. Solve wing (strike, lots) that brings |δ_kg| ≤ MAX_RESIDUAL_KG,
          capped at MAX_LOTS_PER_STEP. Prefer round-thousand strikes.
          BUY CE if book δ<0, BUY PE if book δ>0.
       d. LIMIT@ask → MARKET after 20s (`_limit_then_market`).
       e. On fill: anchor ← F_now, hedges_today += 1.

Guards: emergency stop, session invalid, freeze in the last
`SILVERM_HEDGE_FREEZE_BEFORE_MIN` before STRADDLE_SQUAREOFF_TIME (falls back
to NG_HEDGE_FREEZE_BEFORE_MIN), cooldown between hedges, per-day cap.

State: `data/silverm_hedge_state.json` — resets on date rollover.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime, timedelta
from typing import Any

log = logging.getLogger(__name__)

_LOG_PREFIX = "[silverm_hedge]"
_UNDERLYING = "SILVERM"
_EXCHANGE = "MCX"


def _matches(tradingsymbol: str) -> bool:
    """Prefix = SILVERM AND next char a digit — prevents SILVERMIC collision."""
    if not tradingsymbol.startswith(_UNDERLYING):
        return False
    rest = tradingsymbol[len(_UNDERLYING):]
    return bool(rest) and rest[0].isdigit()


def _state_path(settings: Any) -> str:
    return getattr(settings, "SILVERM_HEDGE_STATE_PATH",
                   "data/silverm_hedge_state.json")


def _load_state(settings: Any) -> dict:
    from app.config import IST
    today = datetime.now(IST).date().isoformat()
    blank = {
        "date": today,
        "F_anchor": None,
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


def _pick_wing(
    session: Any,
    kite: Any,
    opt_expiry: Any,
    opt_type: str,          # "CE" or "PE"
    F: float,
    book_delta_kg: float,
    lot_units: int,
    T: float,
    settings: Any,
) -> tuple[Any, int, float, float] | None:
    """Greedy wing selection: enumerate OTM candidates, pick lowest-lot combo
    that brings residual |δ| ≤ MAX_RESIDUAL_KG. Prefer round-thousand strikes
    (higher OI = tighter spreads on MCX).

    Returns (instrument, lots, wing_delta_per_lot_kg, residual_kg) or None.
    """
    from app.greeks import compute_delta
    from app.orders import throttled_quote_call
    from app.storage import Instrument

    max_residual = float(settings.SILVERM_HEDGE_MAX_RESIDUAL_KG)
    max_lots = int(settings.SILVERM_HEDGE_MAX_LOTS_PER_STEP)
    min_oi = int(settings.SILVERM_HEDGE_WING_MIN_OI)
    min_d = float(settings.SILVERM_HEDGE_WING_MIN_DELTA)
    max_d = float(settings.SILVERM_HEDGE_WING_MAX_DELTA)

    # Fetch every OTM strike of the right type at this expiry from the DB.
    rows = (
        session.query(Instrument)
        .filter(
            Instrument.name == _UNDERLYING,
            Instrument.instrument_type == opt_type,
            Instrument.exchange == _EXCHANGE,
            Instrument.expiry == opt_expiry,
        )
        .all()
    )
    if not rows:
        log.warning("%s no %s instruments in DB for expiry %s", _LOG_PREFIX, opt_type, opt_expiry)
        return None

    # OTM filter: CE strike > F, PE strike < F.
    if opt_type == "CE":
        cands = [r for r in rows if float(r.strike or 0) > F]
    else:
        cands = [r for r in rows if float(r.strike or 0) < F]
    # Order by distance to F (closest OTM first) — cheaper wings, tighter spreads.
    cands.sort(key=lambda r: abs(float(r.strike or 0) - F))
    # Cap to a reasonable radius so one bad LTP call doesn't sprint through the chain.
    cands = cands[:15]
    if not cands:
        log.warning("%s no OTM %s strikes past F=%.0f", _LOG_PREFIX, opt_type, F)
        return None

    # Batch-quote all candidates (LTP + OI). throttled_quote_call keeps us
    # inside the 1 req/sec Kite quote bucket even across hedge/paper/defense.
    full_syms = [f"{_EXCHANGE}:{r.tradingsymbol}" for r in cands]
    try:
        qresp = throttled_quote_call(kite.quote, full_syms)
    except Exception as exc:
        log.warning("%s wing chain quote failed: %s", _LOG_PREFIX, exc)
        return None

    scored: list[tuple[float, int, int, Any, float, float, float]] = []
    # score tuple: (|residual|, lots, -oi_bucket, inst, d_per_lot_kg, residual, cost_per_lot)
    # -oi_bucket lets us prefer higher OI on ties. round-thousand strikes hit
    # the top OI bucket organically.

    need_sign = -1.0 if book_delta_kg > 0 else +1.0  # wing must push toward zero

    for r in cands:
        sym = r.tradingsymbol
        q = qresp.get(f"{_EXCHANGE}:{sym}") or {}
        ltp = float(q.get("last_price") or 0)
        oi = int(q.get("oi") or 0)
        if ltp <= 0:
            continue
        if oi < min_oi:
            log.debug("%s skip %s: OI %d < %d", _LOG_PREFIX, sym, oi, min_oi)
            continue

        res = compute_delta(r, ltp, F, T)
        if res.delta is None:
            continue
        d_unit = res.delta
        # Wing must have delta of the correct sign and within [min_d, max_d].
        if opt_type == "CE" and d_unit <= 0:
            continue
        if opt_type == "PE" and d_unit >= 0:
            continue
        if not (min_d <= abs(d_unit) <= max_d):
            continue

        d_per_lot = d_unit * lot_units  # signed kg per BUY lot
        # BUY sign preserved (long option = its natural δ). Sanity-check direction.
        if d_per_lot * need_sign <= 0:
            continue

        # Enumerate 1..max_lots for this strike; pick the one that best zeroes.
        for lots in range(1, max_lots + 1):
            wing_kg = d_per_lot * lots
            residual = book_delta_kg + wing_kg
            if abs(residual) <= max_residual:
                cost = ltp * lot_units  # per lot
                oi_bucket = -min(oi // 500, 20)  # coarse — 500-OI grid
                scored.append((abs(residual), lots, oi_bucket, r, d_per_lot, residual, cost))
                break  # for this strike, don't add more lots than needed

    if not scored:
        # Nothing hit target — pick the (strike, lots) that minimizes |residual|
        # anyway, still capped at max_lots.
        fallback: list[tuple[float, int, int, Any, float, float, float]] = []
        for r in cands:
            sym = r.tradingsymbol
            q = qresp.get(f"{_EXCHANGE}:{sym}") or {}
            ltp = float(q.get("last_price") or 0)
            oi = int(q.get("oi") or 0)
            if ltp <= 0 or oi < min_oi:
                continue
            res = compute_delta(r, ltp, F, T)
            if res.delta is None:
                continue
            d_unit = res.delta
            if opt_type == "CE" and d_unit <= 0:
                continue
            if opt_type == "PE" and d_unit >= 0:
                continue
            if not (min_d <= abs(d_unit) <= max_d):
                continue
            d_per_lot = d_unit * lot_units
            if d_per_lot * need_sign <= 0:
                continue
            wing_kg = d_per_lot * max_lots
            residual = book_delta_kg + wing_kg
            cost = ltp * lot_units
            oi_bucket = -min(oi // 500, 20)
            fallback.append((abs(residual), max_lots, oi_bucket, r, d_per_lot, residual, cost))
        if not fallback:
            log.warning("%s no usable %s wing found (min_oi=%d, δ∈[%.2f,%.2f])",
                        _LOG_PREFIX, opt_type, min_oi, min_d, max_d)
            return None
        fallback.sort()
        _abs, lots, _oi, inst, d_per_lot, residual, _cost = fallback[0]
        log.info("%s wing FALLBACK (max_lots didn't zero): %s x%d Δ/lot=%+.3fkg residual=%+.3fkg",
                 _LOG_PREFIX, inst.tradingsymbol, lots, d_per_lot, residual)
        return inst, lots, d_per_lot, residual

    scored.sort()
    _abs, lots, _oi, inst, d_per_lot, residual, cost = scored[0]
    log.info("%s wing PICKED: %s x%d Δ/lot=%+.3fkg residual=%+.3fkg cost/lot=₹%.0f",
             _LOG_PREFIX, inst.tradingsymbol, lots, d_per_lot, residual, cost)
    return inst, lots, d_per_lot, residual


def run_silverm_hedge_job(settings: Any, session_factory: Any) -> None:
    """One 1-min wake-up. All exceptions swallowed so the scheduler survives."""
    try:
        _run_impl(settings, session_factory)
    except Exception:
        log.exception("%s tick failed", _LOG_PREFIX)


def _run_impl(settings: Any, session_factory: Any) -> None:
    from app import state
    from app.config import IST
    from app.delta_hedge import _limit_then_market
    from app.kite_session import get_session_manager
    from app.orders import throttled_quote_call
    from app.storage import ClosedTrade, Instrument, Order, Position

    if not state.is_silverm_hedge_enabled(
        getattr(settings, "SILVERM_HEDGE_ENABLED", False)
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
    opts = [
        p for p in positions
        if p.get("exchange") == _EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches(p.get("tradingsymbol") or "")
        and (p["tradingsymbol"].endswith("CE") or p["tradingsymbol"].endswith("PE"))
    ]
    if not opts:
        log.debug("%s no SILVERM options open — nothing to hedge", _LOG_PREFIX)
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

        # Batch: FUT + all open option legs in one quote call.
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
            log.info("%s anchored at F=%.0f (first tick with open book today)",
                     _LOG_PREFIX, F_now)
            _telegram(
                settings,
                f"<b>[SILVERM hedge]</b> anchored at F=₹{F_now:,.0f}/kg\n"
                f"Trigger: any ±₹{settings.SILVERM_HEDGE_TRIGGER_INR:.0f}/kg move "
                f"→ BUY OTM wing sized to |δ| ≤ {settings.SILVERM_HEDGE_MAX_RESIDUAL_KG} kg.",
            )
            return

        # Freeze near the 23:20 squareoff.
        freeze_min = int(getattr(settings, "NG_HEDGE_FREEZE_BEFORE_MIN", 20))
        if freeze_min > 0 and settings.STRADDLE_SQUAREOFF_TIME:
            try:
                sq_h, sq_m = (int(x) for x in settings.STRADDLE_SQUAREOFF_TIME.split(":"))
                freeze_from = (
                    datetime.combine(now.date(), dtime(sq_h, sq_m), tzinfo=IST)
                    - timedelta(minutes=freeze_min)
                )
                if now >= freeze_from:
                    log.info("%s [FREEZE] within %d min of %s — holding (F=%.0f, anchor=%.0f)",
                             _LOG_PREFIX, freeze_min, settings.STRADDLE_SQUAREOFF_TIME,
                             F_now, F_anchor)
                    return
            except ValueError:
                log.warning("%s [FREEZE] bad STRADDLE_SQUAREOFF_TIME=%r",
                            _LOG_PREFIX, settings.STRADDLE_SQUAREOFF_TIME)

        # Per-day cap
        daily_cap = int(getattr(settings, "SILVERM_HEDGE_MAX_HEDGES_PER_DAY", 15))
        if daily_cap > 0 and int(hedge_state.get("hedges_today", 0)) >= daily_cap:
            log.info("%s daily cap %d reached — holding", _LOG_PREFIX, daily_cap)
            return

        # Cooldown
        cooldown_min = int(getattr(settings, "SILVERM_HEDGE_COOLDOWN_MIN", 10))
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
        trigger = float(settings.SILVERM_HEDGE_TRIGGER_INR)
        log.info("%s F=%.0f anchor=%.0f move=%+.0f (trigger ±%.0f)",
                 _LOG_PREFIX, F_now, F_anchor, move, trigger)
        if abs(move) < trigger:
            return

        # ── Compute book δ ──────────────────────────────────────────────────
        from app.greeks import compute_delta

        lot_units = settings.MCX_LOT_UNITS.get(_UNDERLYING, 5)
        close_h, close_m = (int(x) for x in settings.SESSION_CLOSE_MCX.split(":"))

        # Sample expiry from first open leg (assumed uniform per straddle).
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

        total_delta_kg = 0.0
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
            leg_kg = int(p["quantity"]) * d_unit * lot_units
            total_delta_kg += leg_kg
            per_leg_ok = True
            log.info("%s %s %s qty=%+d @ %.1f δ_unit=%+.4f → contrib=%+.3f kg",
                     _LOG_PREFIX, inst.instrument_type, sym, p["quantity"],
                     ltp, d_unit, leg_kg)

        if not per_leg_ok:
            log.warning("%s no leg deltas computed — skipping", _LOG_PREFIX)
            return

        log.info("%s F move ±%.0f BREACHED (%+.0f from %.0f) | net δ=%+.3f kg",
                 _LOG_PREFIX, trigger, move, F_anchor, total_delta_kg)

        # Near-neutral book — nothing worth hedging. Advance anchor so we
        # don't log the same "over trigger" line every minute.
        max_residual = float(settings.SILVERM_HEDGE_MAX_RESIDUAL_KG)
        if abs(total_delta_kg) <= max_residual:
            log.info("%s [SKIP] |δ|=%.3fkg ≤ %.3fkg residual — advancing anchor to F=%.0f",
                     _LOG_PREFIX, abs(total_delta_kg), max_residual, F_now)
            hedge_state["F_anchor"] = F_now
            _save_state(settings, hedge_state)
            return

        # ── Pick wing ───────────────────────────────────────────────────────
        # δ > 0 (book long) → BUY PE (negative δ hedge)
        # δ < 0 (book short) → BUY CE (positive δ hedge)
        opt_type = "PE" if total_delta_kg > 0 else "CE"
        picked = _pick_wing(session, kite, opt_expiry, opt_type, F_now,
                            total_delta_kg, lot_units, T, settings)
        if picked is None:
            log.warning("%s no wing available — holding (no anchor advance)", _LOG_PREFIX)
            _telegram(
                settings,
                f"<b>[SILVERM hedge · SKIPPED]</b> no {opt_type} wing available\n"
                f"F: {F_anchor:.0f} → {F_now:.0f} ({move:+.0f})\n"
                f"δ: {total_delta_kg:+.2f} kg | need {opt_type} lot",
            )
            return
        wing_inst, lots, d_per_lot_kg, residual_kg = picked

        log.info(
            "%s HEDGE PLAN: BUY %d lot %s (Δ/lot=%+.3fkg) → residual δ=%+.3fkg",
            _LOG_PREFIX, lots, wing_inst.tradingsymbol, d_per_lot_kg, residual_kg,
        )

        if settings.DRY_RUN:
            log.info("%s DRY_RUN — order not placed", _LOG_PREFIX)
            _telegram(
                settings,
                f"<b>[SILVERM hedge · DRY]</b> would BUY {lots} lot {wing_inst.tradingsymbol}\n"
                f"F: {F_anchor:.0f} → {F_now:.0f} ({move:+.0f})\n"
                f"δ before: {total_delta_kg:+.2f} kg → est after: {residual_kg:+.2f} kg",
            )
            return

        qty = lots * wing_inst.lot_size  # MCX option lot_size=1
        oid, mode = _limit_then_market(kite, wing_inst, "BUY", qty, settings)
        if oid is None:
            log.error("%s hedge failed (mode=%s) — anchor NOT advanced",
                      _LOG_PREFIX, mode)
            _telegram(
                settings,
                f"<b>[SILVERM hedge · FAILED]</b>\n"
                f"F: {F_anchor:.0f} → {F_now:.0f} ({move:+.0f})\n"
                f"δ: {total_delta_kg:+.2f} kg | mode: {mode}\n"
                f"Wanted: BUY {lots} lot {wing_inst.tradingsymbol}. Manual attention.",
            )
            return

        # Mirror to Position table so EOD squareoff sees the new long. If no
        # Position row exists (fresh strike), squareoff still closes via
        # kite.positions().
        pos = (
            session.query(Position)
            .join(Order, Position.order_id == Order.id)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol == wing_inst.tradingsymbol,
                Position.exchange == _EXCHANGE,
                ClosedTrade.id == None,  # noqa: E711
                Order.dry_run == False,  # noqa: E712
            )
            .first()
        )
        if pos is not None:
            old_qty = pos.quantity
            pos.quantity = old_qty + qty  # BUY adds (more positive)
            pos.last_updated_at = now
            session.commit()
            log.info("%s Position.id=%d qty %d→%d (BUY %d)",
                     _LOG_PREFIX, pos.id, old_qty, pos.quantity, qty)
        else:
            log.warning(
                "%s no open Position row for %s — squareoff will still close it "
                "via kite.positions",
                _LOG_PREFIX, wing_inst.tradingsymbol,
            )

        # Advance state on successful fill.
        hedge_state["F_anchor"] = F_now
        hedge_state["hedges_today"] = int(hedge_state.get("hedges_today", 0)) + 1
        hedge_state["last_hedge_at"] = now.isoformat()
        _save_state(settings, hedge_state)

        _telegram(
            settings,
            f"<b>[SILVERM hedge]</b> BUY {lots} lot "
            f"{wing_inst.tradingsymbol} ({mode})\n"
            f"F: {F_anchor:.0f} → {F_now:.0f} ({move:+.0f})\n"
            f"δ before: {total_delta_kg:+.2f} kg → est after: {residual_kg:+.2f} kg\n"
            f"hedge #{hedge_state['hedges_today']} today | oid: {oid}\n"
            f"new anchor: {F_now:.0f}",
        )
