"""Straddle defense — monitor short straddles, alert on IV expansion, and
(Phase 2) hedge them with long OTM wings.

Every minute during MCX hours (cron in scheduler.py, gated live by the
/control toggle) the job:

  1. Computes per-straddle MTM + mean leg IV via compute_portfolio_greeks
     (one quote round, shared code path with /desk and /control).
  2. Samples both into the iv_snapshots table — the IV series is the
     expansion-confirmation signal; MTM feeds the drawdown trigger.
  3. Tracks each straddle's peak MTM for the day (persisted to
     data/straddle_defense_state.json so restarts don't blind the trigger).
  4. Fires a Telegram alert when BOTH hold:
       drawdown-from-peak >= STRADDLE_DEFENSE_DRAWDOWN_TRIGGER
       AND IV rose for STRADDLE_DEFENSE_IV_SAMPLES consecutive samples.
     Hysteresis: max STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY alerts per straddle
     and a STRADDLE_DEFENSE_REARM_MINUTES quiet period between them.
  5. Pre-hedge window at STRADDLE_DEFENSE_PREHEDGE_TIME — the cheap moment to
     buy wings, before the evening IV ramp.

Phase 2 execution — mode is ALERT (default), SEMI_AUTO, or AUTO
(state.get_straddle_defense_mode; AUTO additionally requires the env-only
STRADDLE_DEFENSE_AUTO_EXECUTE flag or it degrades to SEMI_AUTO):

  * A hedge = BUY CE + BUY PE wings STRADDLE_DEFENSE_WING_STEPS strike
    intervals outside the short strikes, same expiry and quantity — the
    straddle becomes an iron butterfly with capped loss for the IV spike.
  * SEMI_AUTO builds a proposal (HedgeAction row, PROPOSED) and asks for a
    two-tap approval on /control; TTL STRADDLE_DEFENSE_PROPOSAL_TTL_MINUTES.
    AUTO places immediately. Both respect the ₹/day budget cap
    STRADDLE_DEFENSE_MAX_HEDGE_COST.
  * While wings are on, the short legs' GTT SLs are SUSPENDED (cancelled on
    Kite, params kept in hedge_actions.suspended_gtts) so the inflated
    premium can't stop the shorts out — the wings cap the risk instead.
    They are re-placed verbatim at unwind.
  * Unwind: scheduled at STRADDLE_DEFENSE_UNWIND_TIME (AUTO unwinds itself,
    SEMI_AUTO gets a Telegram nudge + /control button), force-unwound for
    every mode at STRADDLE_DEFENSE_FORCE_UNWIND_TIME so the 23:20 straddle
    squareoff never runs while stops are suspended. Wing positions are also
    ordinary Position rows, so the EOD squareoff is a final backstop.

Trigger logic stays in pure functions (iv_rising / evaluate_straddle);
scripts/replay_straddle_defense.py replays recorded iv_snapshots through them
to tune thresholds.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app import state
from app.config import IST

log = logging.getLogger(__name__)

_PREFIX = "[straddle_defense]"
_STATE_PATH = os.path.join("data", "straddle_defense_state.json")
_SNAPSHOT_RETENTION_DAYS = 30
_IV_SERIES_WINDOW = 60          # samples pulled for slope evaluation / card

_lock = threading.Lock()
_state: dict | None = None      # {"date": "YYYY-MM-DD", "prehedge_sent": bool,
                                #  "straddles": {key: {"peak": f, "alerts": n,
                                #                       "last_alert": iso|None}}}


# ── persisted day-state ───────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        with open(_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
    except OSError as exc:
        log.warning("%s could not persist state: %s", _PREFIX, exc)


def _today_state(now: datetime) -> dict:
    """Return the mutable day-state dict, resetting peaks on date change."""
    global _state
    with _lock:
        if _state is None:
            _state = _load_state()
        today = now.date().isoformat()
        if _state.get("date") != today:
            _state = {"date": today, "prehedge_sent": False, "straddles": {}}
        return _state


def reset_state_for_tests() -> None:
    global _state
    with _lock:
        _state = None


# ── pure trigger logic ────────────────────────────────────────────────────────

def iv_rising(samples: list[float], n: int) -> bool:
    """True when the last n deltas of the IV series are all positive
    (needs n+1 samples). One flat or falling step breaks the streak —
    this is the noise filter, not a trend model."""
    if n <= 0 or len(samples) < n + 1:
        return False
    tail = samples[-(n + 1):]
    return all(b > a for a, b in zip(tail, tail[1:]))


def evaluate_straddle(
    key: str,
    mtm: float,
    iv_series: list[float],
    settings: Any,
    st: dict,
    now: datetime,
) -> float | None:
    """Update the straddle's peak; return the drawdown when the reactive
    trigger fires (respecting hysteresis), else None. Mutates st."""
    rec = st["straddles"].setdefault(key, {"peak": mtm, "alerts": 0, "last_alert": None})
    if mtm > rec["peak"]:
        rec["peak"] = mtm
    drawdown = rec["peak"] - mtm

    if drawdown < settings.STRADDLE_DEFENSE_DRAWDOWN_TRIGGER:
        return None
    if not iv_rising(iv_series, settings.STRADDLE_DEFENSE_IV_SAMPLES):
        return None
    if rec["alerts"] >= settings.STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY:
        return None
    if rec["last_alert"] is not None:
        last = datetime.fromisoformat(rec["last_alert"])
        if (now - last).total_seconds() < settings.STRADDLE_DEFENSE_REARM_MINUTES * 60:
            return None

    rec["alerts"] += 1
    rec["last_alert"] = now.isoformat()
    return drawdown


# ── Phase 2: wing hedge execution ─────────────────────────────────────────────

def effective_mode(settings: Any) -> str:
    """Live mode with the AUTO hard gate applied: AUTO without the env-only
    STRADDLE_DEFENSE_AUTO_EXECUTE flag degrades to SEMI_AUTO."""
    mode = state.get_straddle_defense_mode(settings.STRADDLE_DEFENSE_MODE)
    if mode == "AUTO" and not settings.STRADDLE_DEFENSE_AUTO_EXECUTE:
        return "SEMI_AUTO"
    return mode


def _units_multiplier(settings: Any, exchange: str, underlying: str) -> int:
    """₹-per-point multiplier for one Kite qty unit. MCX options carry
    lot_size=1 in the instrument dump; MCX_LOT_UNITS holds the real size."""
    return settings.MCX_LOT_UNITS.get(underlying, 1) if exchange.startswith("MCX") else 1


def _chain_interval(session: Any, underlying: str, expiry: Any, exchange: str,
                    settings: Any) -> float:
    """Strike spacing derived from the actual option chain (min positive gap
    between listed CE strikes); config fallback when the chain is too thin."""
    from app.storage import Instrument

    strikes = sorted({
        float(s) for (s,) in session.query(Instrument.strike)
        .filter(Instrument.name == underlying, Instrument.expiry == expiry,
                Instrument.instrument_type == "CE", Instrument.exchange == exchange)
        .all() if s
    })
    diffs = [round(b - a, 2) for a, b in zip(strikes, strikes[1:]) if b - a > 0]
    if diffs:
        return min(diffs)
    return settings.STRADDLE_STRIKE_INTERVALS.get(underlying, settings.STRADDLE_STRIKE_INTERVAL)


def _find_wing(session: Any, underlying: str, expiry: Any, exchange: str, flag: str,
               short_strike: float, interval: float, steps: int) -> Any:
    """Instrument for the wing: `steps` intervals OTM from the short strike
    (CE above, PE below). Tries one interval further out, then one closer in,
    when the exact strike isn't listed."""
    from app.storage import Instrument

    sign = 1.0 if flag == "CE" else -1.0
    for k in [steps, steps + 1, max(steps - 1, 1)]:
        target = round(short_strike + sign * k * interval, 2)
        inst = (
            session.query(Instrument)
            .filter(Instrument.name == underlying, Instrument.expiry == expiry,
                    Instrument.instrument_type == flag, Instrument.strike == target,
                    Instrument.exchange == exchange)
            .first()
        )
        if inst is not None:
            return inst
    return None


def select_wings(session: Any, settings: Any, straddle_key: str) -> dict | None:
    """Build the wing plan for a short straddle: {underlying, exchange, expiry,
    quantity, ce, pe} with Instrument objects, or None with a logged reason."""
    from app.storage import ClosedTrade, Instrument, Order, Position

    rows = (
        session.query(Position, Order)
        .join(Order, Position.order_id == Order.id)
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(Order.straddle_id == straddle_key,
                Order.transaction_type == "SELL",
                ClosedTrade.id == None)  # noqa: E711
        .all()
    )
    legs: dict[str, tuple[Any, Any]] = {}
    for pos, order in rows:
        if pos.instrument_type in ("CE", "PE"):
            legs[pos.instrument_type] = (pos, order)
    if "CE" not in legs or "PE" not in legs:
        log.warning("%s select_wings %s: short CE+PE pair not found (open legs=%s)",
                    _PREFIX, straddle_key, sorted(legs))
        return None

    def _inst_for(pos: Any) -> Any:
        return (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == pos.tradingsymbol,
                    Instrument.exchange == pos.exchange)
            .first()
        )

    ce_pos, _ = legs["CE"]
    pe_pos, _ = legs["PE"]
    ce_short = _inst_for(ce_pos)
    pe_short = _inst_for(pe_pos)
    if ce_short is None or pe_short is None or not ce_short.expiry:
        log.warning("%s select_wings %s: short-leg instruments missing", _PREFIX, straddle_key)
        return None

    exchange = ce_pos.exchange
    underlying = ce_pos.underlying
    interval = _chain_interval(session, ce_short.name, ce_short.expiry, exchange, settings)
    steps = max(int(settings.STRADDLE_DEFENSE_WING_STEPS), 1)
    ce_wing = _find_wing(session, ce_short.name, ce_short.expiry, exchange, "CE",
                         float(ce_short.strike), interval, steps)
    pe_wing = _find_wing(session, pe_short.name, pe_short.expiry, exchange, "PE",
                         float(pe_short.strike), interval, steps)
    if ce_wing is None or pe_wing is None:
        log.warning("%s select_wings %s: no wing strikes near CE %.2f / PE %.2f (interval %.2f)",
                    _PREFIX, straddle_key, ce_short.strike, pe_short.strike, interval)
        return None

    return {
        "underlying": underlying,
        "exchange": exchange,
        "expiry": ce_short.expiry,
        "quantity": max(ce_pos.quantity, pe_pos.quantity),
        "ce": ce_wing,
        "pe": pe_wing,
    }


def _wing_prices(kite: Any, exchange: str, ce_symbol: str, pe_symbol: str) -> tuple[float, float]:
    """Best-effort (ask, else LTP) per wing; 0.0 when unavailable."""
    def _px(q: dict) -> float:
        sell_depth = (q.get("depth") or {}).get("sell") or []
        if sell_depth and float(sell_depth[0].get("price") or 0) > 0:
            return float(sell_depth[0]["price"])
        return float(q.get("last_price") or 0.0)

    try:
        keys = [f"{exchange}:{ce_symbol}", f"{exchange}:{pe_symbol}"]
        quotes = kite.quote(keys)
        return _px(quotes.get(keys[0]) or {}), _px(quotes.get(keys[1]) or {})
    except Exception as exc:
        log.warning("%s wing quote failed: %s", _PREFIX, exc)
        return 0.0, 0.0


def hedge_cost_today(session: Any, now: datetime) -> float:
    """₹ of hedge premium committed today — executed cost where known, else the
    proposal estimate. REJECTED/EXPIRED rows release their budget."""
    from app.storage import HedgeAction

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for a in (session.query(HedgeAction)
              .filter(HedgeAction.proposed_at >= day_start,
                      HedgeAction.status.in_(["PROPOSED", "ACTIVE", "UNWOUND", "FAILED"]))
              .all()):
        total += a.entry_cost if a.entry_cost is not None else (a.est_cost or 0.0)
    return total


def get_hedge(session: Any, straddle_key: str, statuses: tuple[str, ...]) -> Any:
    from app.storage import HedgeAction

    return (
        session.query(HedgeAction)
        .filter(HedgeAction.straddle_key == straddle_key,
                HedgeAction.status.in_(list(statuses)))
        .order_by(HedgeAction.id.desc())
        .first()
    )


def propose_hedge(session: Any, settings: Any, straddle_key: str, trigger: str,
                  now: datetime, mode: str, kite: Any = None) -> Any:
    """Create a PROPOSED HedgeAction for the straddle (wings + cost estimate),
    guarded by active/pending dedup and the daily budget cap. Returns the row
    or None. Caller commits; AUTO callers follow with execute_hedge."""
    from app.commodity_agents.notify import send_telegram
    from app.storage import HedgeAction

    if get_hedge(session, straddle_key, ("PROPOSED", "ACTIVE")) is not None:
        log.info("%s propose %s: already proposed/active — skipping", _PREFIX, straddle_key)
        return None

    plan = select_wings(session, settings, straddle_key)
    if plan is None:
        return None

    dry = state.is_paper_mode(settings.DRY_RUN)
    if kite is None:
        try:
            from app.kite_session import get_session_manager
            kite = get_session_manager().get_kite()
        except Exception:
            kite = None
    if kite is None and not dry:
        log.warning("%s propose %s: no Kite session — cannot price wings", _PREFIX, straddle_key)
        return None

    units = _units_multiplier(settings, plan["exchange"], plan["underlying"])
    ce_px, pe_px = _wing_prices(kite, plan["exchange"], plan["ce"].tradingsymbol,
                                plan["pe"].tradingsymbol) if kite else (0.0, 0.0)
    est_cost = (ce_px + pe_px) * plan["quantity"] * units

    budget = settings.STRADDLE_DEFENSE_MAX_HEDGE_COST
    spent = hedge_cost_today(session, now)
    if budget > 0 and est_cost > 0 and spent + est_cost > budget:
        send_telegram(settings, (
            f"\U0001f6d1 <b>{plan['underlying']} hedge blocked — budget cap</b>\n"
            f"Wings would cost ₹{est_cost:,.0f}; ₹{spent:,.0f} of "
            f"₹{budget:,.0f} already committed today. Alert-only."
        ))
        log.warning("%s propose %s: budget cap (spent %.0f + est %.0f > %.0f)",
                    _PREFIX, straddle_key, spent, est_cost, budget)
        return None

    action = HedgeAction(
        straddle_key=straddle_key,
        underlying=plan["underlying"],
        exchange=plan["exchange"],
        mode=mode,
        trigger=trigger,
        status="PROPOSED",
        ce_symbol=plan["ce"].tradingsymbol,
        pe_symbol=plan["pe"].tradingsymbol,
        quantity=plan["quantity"],
        est_cost=round(est_cost, 2),
        confirm_token=secrets.token_urlsafe(16),
        proposed_at=now,
        expires_at=now + timedelta(minutes=settings.STRADDLE_DEFENSE_PROPOSAL_TTL_MINUTES),
        dry_run=dry,
    )
    session.add(action)
    session.flush()

    if mode != "AUTO":
        send_telegram(settings, (
            f"\U0001f6e1 <b>{plan['underlying']} wing hedge proposed</b> ({trigger})\n"
            f"BUY {plan['ce'].tradingsymbol} + BUY {plan['pe'].tradingsymbol} "
            f"qty {plan['quantity']} ≈ ₹{est_cost:,.0f}\n"
            f"Approve on /control within "
            f"{settings.STRADDLE_DEFENSE_PROPOSAL_TTL_MINUTES} min."
        ))
    log.warning("%s PROPOSED hedge #%d %s: %s + %s qty=%d est=₹%.0f (%s/%s)",
                _PREFIX, action.id, plan["underlying"], action.ce_symbol,
                action.pe_symbol, action.quantity, est_cost, mode, trigger)
    return action


def _suspend_gtts(session: Any, kite: Any, straddle_key: str, now: datetime,
                  dry: bool) -> list[dict]:
    """Cancel the short legs' ACTIVE GTTs on Kite and mark the rows SUSPENDED.
    Returns restore-parameter dicts (persisted on the HedgeAction). A GTT whose
    Kite cancel fails is left ACTIVE and not recorded."""
    from app.orders import cancel_gtt
    from app.storage import Gtt, Order

    recs: list[dict] = []
    rows = (
        session.query(Gtt, Order)
        .join(Order, Gtt.order_id == Order.id)
        .filter(Order.straddle_id == straddle_key, Gtt.status == "ACTIVE")
        .all()
    )
    for gtt, order in rows:
        if not dry and gtt.kite_gtt_id:
            try:
                cancel_gtt(kite, gtt.kite_gtt_id)
            except Exception as exc:
                log.error("%s suspend: cancel GTT %s failed (%s) — leaving ACTIVE",
                          _PREFIX, gtt.kite_gtt_id, exc)
                continue
        gtt.status = "SUSPENDED"
        gtt.updated_at = now
        recs.append({
            "order_id": gtt.order_id,
            "tradingsymbol": gtt.tradingsymbol,
            "exchange": gtt.exchange,
            "quantity": order.quantity,
            "product": order.product,
            "sl_trigger": gtt.sl_trigger,
            "target_trigger": gtt.target_trigger,
            "sl_order_price": gtt.sl_order_price,
            "target_order_price": gtt.target_order_price,
            "last_price_at_placement": gtt.last_price_at_placement,
        })
    return recs


def _restore_gtts(session: Any, settings: Any, kite: Any, recs: list[dict],
                  now: datetime) -> int:
    """Re-place suspended GTT OCOs verbatim for short legs that are still open.
    Failures alert loudly — a short without its stop needs the human."""
    from app.commodity_agents.notify import send_telegram
    from app.orders import place_gtt_oco
    from app.storage import ClosedTrade, Gtt, Instrument, Position

    restored = 0
    for rec in recs:
        pos = (
            session.query(Position)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(Position.order_id == rec["order_id"],
                    ClosedTrade.id == None)  # noqa: E711
            .first()
        )
        if pos is None:
            continue   # short already closed — nothing to protect
        inst = (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == rec["tradingsymbol"],
                    Instrument.exchange == rec["exchange"])
            .first()
        )
        try:
            if inst is None:
                raise RuntimeError("instrument row missing")
            key = f"{rec['exchange']}:{rec['tradingsymbol']}"
            try:
                last = float(kite.quote([key])[key]["last_price"])
            except Exception:
                last = float(rec["last_price_at_placement"])
            kite_gtt_id = place_gtt_oco(
                kite, inst, rec["quantity"],
                Decimal(str(rec["sl_trigger"])), Decimal(str(rec["sl_order_price"])),
                Decimal(str(rec["target_trigger"])), Decimal(str(rec["target_order_price"])),
                Decimal(str(last)), rec["product"], entry_side="SELL",
            )
            new_gtt = Gtt(
                order_id=rec["order_id"], kite_gtt_id=kite_gtt_id, gtt_type="OCO",
                exchange=rec["exchange"], tradingsymbol=rec["tradingsymbol"],
                sl_trigger=rec["sl_trigger"], target_trigger=rec["target_trigger"],
                sl_order_price=rec["sl_order_price"],
                target_order_price=rec["target_order_price"],
                last_price_at_placement=last, status="ACTIVE",
                placed_at=now, updated_at=now, dry_run=False,
            )
            session.add(new_gtt)
            session.flush()
            pos.gtt_id = new_gtt.id
            pos.current_sl = rec["sl_trigger"]
            pos.last_updated_at = now
            restored += 1
        except Exception as exc:
            log.error("%s restore GTT for %s failed: %s", _PREFIX, rec["tradingsymbol"], exc)
            send_telegram(settings, (
                f"⚠️ <b>GTT restore FAILED</b> for {rec['tradingsymbol']} "
                f"(SL {rec['sl_trigger']}) after wing unwind: {exc}\n"
                f"The short leg has NO stop — place a GTT manually."
            ))
    return restored


def execute_hedge(session: Any, settings: Any, action: Any, now: datetime) -> bool:
    """Place both wing BUYs (market, concurrent), suspend the short-leg GTTs,
    and record Order + Position rows. Paper mode records DRY_RUN orders only.
    Caller commits. Returns True when the action went ACTIVE."""
    from concurrent.futures import ThreadPoolExecutor

    from app.commodity_agents.notify import send_telegram
    from app.orders import place_entry, square_off
    from app.storage import Alert, Instrument, Order, Position

    dry = state.is_paper_mode(settings.DRY_RUN)
    product = settings.PRODUCT_TYPE.value
    units = _units_multiplier(settings, action.exchange, action.underlying)

    def _inst(sym: str) -> Any:
        return (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == sym, Instrument.exchange == action.exchange)
            .first()
        )

    ce_inst, pe_inst = _inst(action.ce_symbol), _inst(action.pe_symbol)
    if ce_inst is None or pe_inst is None:
        action.status = "FAILED"
        log.error("%s execute #%d: wing instruments missing", _PREFIX, action.id)
        return False

    kite = None
    ce_oid = pe_oid = None
    if not dry:
        try:
            from app.kite_session import get_session_manager
            kite = get_session_manager().get_kite()
        except Exception as exc:
            action.status = "FAILED"
            log.error("%s execute #%d: no Kite session: %s", _PREFIX, action.id, exc)
            return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_ce = pool.submit(place_entry, kite, ce_inst, "BUY", action.quantity,
                                 "MARKET", product)
            fut_pe = pool.submit(place_entry, kite, pe_inst, "BUY", action.quantity,
                                 "MARKET", product)
            ce_err = pe_err = None
            try:
                ce_oid = fut_ce.result(timeout=settings.STRADDLE_FILL_TIMEOUT_SECS)
            except Exception as exc:
                ce_err = exc
            try:
                pe_oid = fut_pe.result(timeout=settings.STRADDLE_FILL_TIMEOUT_SECS)
            except Exception as exc:
                pe_err = exc

        if bool(ce_oid) != bool(pe_oid):   # one wing failed — reverse the other
            good_inst, good_label = (ce_inst, "CE") if ce_oid else (pe_inst, "PE")
            err = pe_err if ce_oid else ce_err
            log.error("%s execute #%d: %s wing failed (%s) — reversing %s wing",
                      _PREFIX, action.id, "PE" if ce_oid else "CE", err, good_label)
            try:
                square_off(kite, good_inst, action.quantity, product, entry_side="BUY")
            except Exception as exc:
                log.error("%s execute #%d: CRITICAL — cannot reverse lone %s wing: %s "
                          "— MANUAL INTERVENTION REQUIRED", _PREFIX, action.id, good_label, exc)
            action.status = "FAILED"
            send_telegram(settings, (
                f"❌ <b>{action.underlying} wing hedge FAILED</b> — one leg "
                f"rejected ({err}); the filled wing was reversed."
            ))
            return False
        if not ce_oid and not pe_oid:
            action.status = "FAILED"
            log.error("%s execute #%d: both wings failed (CE %s / PE %s)",
                      _PREFIX, action.id, ce_err, pe_err)
            send_telegram(settings, (
                f"❌ <b>{action.underlying} wing hedge FAILED</b> — both legs "
                f"rejected (CE: {ce_err} / PE: {pe_err})."
            ))
            return False

    # fill prices — best effort from the order book, fall back to fresh quotes
    ce_fill = pe_fill = 0.0
    if not dry:
        try:
            import time as _time
            _time.sleep(1.5)
            by_id = {o.get("order_id"): o for o in (kite.orders() or [])}
            ce_fill = float((by_id.get(ce_oid) or {}).get("average_price") or 0.0)
            pe_fill = float((by_id.get(pe_oid) or {}).get("average_price") or 0.0)
        except Exception as exc:
            log.warning("%s execute #%d: order-book fill fetch failed: %s",
                        _PREFIX, action.id, exc)
    if (ce_fill <= 0 or pe_fill <= 0) and kite is not None:
        q_ce, q_pe = _wing_prices(kite, action.exchange, action.ce_symbol, action.pe_symbol)
        ce_fill = ce_fill or q_ce
        pe_fill = pe_fill or q_pe

    alert = Alert(
        received_at=now,
        strategy_id=f"sd_hedge_{action.underlying}",
        tv_ticker=action.underlying,
        tv_exchange=action.exchange,
        action="HEDGE",
        order_type="MARKET",
        product=product,
        idempotency_key=f"sdh-{uuid.uuid4()}",
        raw_payload=json.dumps({
            "hedge_action_id": action.id, "trigger": action.trigger,
            "ce": action.ce_symbol, "pe": action.pe_symbol, "qty": action.quantity,
        }),
        processed=True,
    )
    session.add(alert)
    session.flush()

    for inst, kite_oid, fill in [(ce_inst, ce_oid, ce_fill), (pe_inst, pe_oid, pe_fill)]:
        o = Order(
            alert_id=alert.id, kite_order_id=kite_oid, variety="regular",
            exchange=action.exchange, tradingsymbol=inst.tradingsymbol,
            transaction_type="BUY", order_type="MARKET", product=product,
            quantity=action.quantity, status="DRY_RUN" if dry else "COMPLETE",
            fill_price=fill or None, fill_qty=None if dry else action.quantity,
            placed_at=now, updated_at=now, dry_run=dry,
        )
        session.add(o)
        session.flush()
        if not dry:
            session.add(Position(
                order_id=o.id, exchange=action.exchange,
                tradingsymbol=inst.tradingsymbol, underlying=action.underlying,
                instrument_type=inst.instrument_type, entry_premium=fill or 0.0,
                current_sl=0.0, quantity=action.quantity, lot_size=inst.lot_size,
                opened_at=now, last_updated_at=now,
            ))

    suspended: list[dict] = []
    if not dry and settings.STRADDLE_DEFENSE_SUSPEND_GTT:
        suspended = _suspend_gtts(session, kite, action.straddle_key, now, dry)

    entry_cost = (ce_fill + pe_fill) * action.quantity * units
    action.status = "ACTIVE"
    action.executed_at = now
    action.entry_cost = round(entry_cost, 2) if entry_cost > 0 else action.est_cost
    action.ce_order_id = ce_oid
    action.pe_order_id = pe_oid
    action.suspended_gtts = json.dumps(suspended) if suspended else None
    action.dry_run = dry

    send_telegram(settings, (
        f"\U0001f6e1 <b>{action.underlying} wings ON</b>"
        f"{' (paper)' if dry else ''} — {action.trigger}\n"
        f"BUY {action.ce_symbol} + BUY {action.pe_symbol} qty {action.quantity} "
        f"cost ₹{action.entry_cost:,.0f}\n"
        f"Short-leg SLs suspended: {len(suspended)}. Straddle is now an iron "
        f"butterfly — loss capped for the IV spike."
    ))
    log.warning("%s ACTIVE hedge #%d %s cost=₹%.0f suspended_gtts=%d dry=%s",
                _PREFIX, action.id, action.underlying, action.entry_cost or 0,
                len(suspended), dry)
    return True


def decide_hedge(session: Any, settings: Any, action_id: int, token: str,
                 approve: bool, now: datetime) -> tuple[bool, str]:
    """Two-tap outcome for a PROPOSED hedge. Caller commits."""
    from app.storage import HedgeAction

    action = session.get(HedgeAction, action_id)
    if action is None or action.status != "PROPOSED":
        return False, "proposal not found or already decided"
    if not token or token != (action.confirm_token or ""):
        return False, "bad confirm token"
    if action.expires_at is not None and now > action.expires_at:
        action.status = "EXPIRED"
        return False, "proposal expired"
    if not approve:
        action.status = "REJECTED"
        log.info("%s hedge #%d rejected by user", _PREFIX, action.id)
        return True, "rejected"
    ok = execute_hedge(session, settings, action, now)
    return ok, "executed" if ok else "execution failed"


def unwind_hedge(session: Any, settings: Any, action: Any, now: datetime,
                 reason: str = "manual") -> bool:
    """Sell both wings, close their Position rows, restore the suspended GTTs,
    and finalize the HedgeAction. Caller commits."""
    from app.commodity_agents.notify import send_telegram
    from app.orders import square_off
    from app.storage import ClosedTrade, Instrument, Order, Position

    if action.status != "ACTIVE":
        return False
    dry = bool(action.dry_run)
    product = settings.PRODUCT_TYPE.value
    units = _units_multiplier(settings, action.exchange, action.underlying)

    kite = None
    if not dry:
        try:
            from app.kite_session import get_session_manager
            kite = get_session_manager().get_kite()
        except Exception as exc:
            log.error("%s unwind #%d: no Kite session: %s", _PREFIX, action.id, exc)
            return False

    exit_px: dict[str, float] = {}
    if kite is not None:
        ce_px, pe_px = _wing_prices(kite, action.exchange, action.ce_symbol, action.pe_symbol)
        exit_px = {action.ce_symbol: ce_px, action.pe_symbol: pe_px}

    for sym in (action.ce_symbol, action.pe_symbol):
        inst = (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == sym, Instrument.exchange == action.exchange)
            .first()
        )
        if not dry and inst is not None:
            try:
                square_off(kite, inst, action.quantity, product, entry_side="BUY")
            except Exception as exc:
                log.error("%s unwind #%d: square-off %s failed: %s", _PREFIX, action.id, sym, exc)
                send_telegram(settings, (
                    f"⚠️ <b>{action.underlying} wing unwind</b>: SELL {sym} "
                    f"failed ({exc}) — close it manually."
                ))

    # close the wing Position rows (kite order id → Order row → Position)
    for kite_oid in (action.ce_order_id, action.pe_order_id):
        if not kite_oid:
            continue
        row = (
            session.query(Position, Order)
            .join(Order, Position.order_id == Order.id)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(Order.kite_order_id == kite_oid,
                    ClosedTrade.id == None)  # noqa: E711
            .first()
        )
        if row is None:
            continue
        pos, order = row
        px = exit_px.get(pos.tradingsymbol, 0.0)
        session.add(ClosedTrade(
            position_id=pos.id, exchange=pos.exchange, tradingsymbol=pos.tradingsymbol,
            entry_premium=pos.entry_premium, exit_premium=px,
            pnl=round((px - pos.entry_premium) * pos.quantity * units, 2),
            exit_reason="HEDGE_UNWIND", opened_at=pos.opened_at, closed_at=now,
        ))

    restored = 0
    if not dry and action.suspended_gtts:
        try:
            recs = json.loads(action.suspended_gtts)
        except ValueError:
            recs = []
        restored = _restore_gtts(session, settings, kite, recs, now)

    exit_value = sum(exit_px.values()) * action.quantity * units if exit_px else None
    action.status = "UNWOUND"
    action.unwound_at = now
    action.exit_value = round(exit_value, 2) if exit_value else None
    if action.exit_value is not None and action.entry_cost is not None:
        action.pnl = round(action.exit_value - action.entry_cost, 2)

    send_telegram(settings, (
        f"\U0001f3c1 <b>{action.underlying} wings OFF</b>"
        f"{' (paper)' if dry else ''} — {reason}\n"
        f"SOLD {action.ce_symbol} + {action.pe_symbol}"
        + (f", recovered ₹{action.exit_value:,.0f}"
           f" (hedge P&L ₹{action.pnl:+,.0f})" if action.pnl is not None else "")
        + f"\nGTT stops restored: {restored}."
    ))
    log.warning("%s UNWOUND hedge #%d %s reason=%s pnl=%s restored=%d",
                _PREFIX, action.id, action.underlying, reason, action.pnl, restored)
    return True


def _hhmm_to_min(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _maybe_unwind_scheduled(session: Any, settings: Any, st: dict, now: datetime,
                            mode: str) -> None:
    """Scheduled unwind at UNWIND_TIME (AUTO acts, SEMI_AUTO gets one nudge)
    and unconditional force-unwind at FORCE_UNWIND_TIME for every mode."""
    from app.commodity_agents.notify import send_telegram
    from app.storage import HedgeAction

    actives = session.query(HedgeAction).filter(HedgeAction.status == "ACTIVE").all()
    if not actives:
        return
    now_min = now.hour * 60 + now.minute

    force_min = _hhmm_to_min(settings.STRADDLE_DEFENSE_FORCE_UNWIND_TIME)
    if force_min is not None and now_min >= force_min:
        for a in actives:
            unwind_hedge(session, settings, a, now, reason="force_eod")
        return

    sched_min = _hhmm_to_min(settings.STRADDLE_DEFENSE_UNWIND_TIME)
    if sched_min is None or not (sched_min <= now_min < sched_min + 60):
        return
    if mode == "AUTO":
        for a in actives:
            unwind_hedge(session, settings, a, now, reason="scheduled")
    elif not st.get("unwind_nudged"):
        st["unwind_nudged"] = True
        names = ", ".join(sorted({a.underlying for a in actives}))
        send_telegram(settings, (
            f"\U0001f552 <b>Cool-off over</b> ({settings.STRADDLE_DEFENSE_UNWIND_TIME} IST)\n"
            f"Wings still on: {names}. IV ramp should be done — unwind on /control "
            f"to stop paying theta on the longs."
        ))


# ── the 1-min job ─────────────────────────────────────────────────────────────

def straddle_defense_job(settings: Any, session_factory: Any) -> None:
    if not state.is_straddle_defense_enabled(settings.STRADDLE_DEFENSE_ENABLED):
        return

    from app.commodity_agents.notify import send_telegram
    from app.commodity_agents.portfolio import compute_portfolio_greeks
    from app.kite_session import get_session_manager
    from app.storage import IvSnapshot

    now = datetime.now(IST)
    try:
        kite = get_session_manager().get_kite()
    except Exception:
        return  # no session — nothing to monitor

    try:
        with session_factory() as session:
            from app.storage import HedgeAction

            greeks = compute_portfolio_greeks(session, kite, settings, now=now)
            shorts = {k: g for k, g in (greeks.get("straddles") or {}).items()
                      if g.get("short")}
            st = _today_state(now)
            mode = effective_mode(settings)

            # lapse stale SEMI_AUTO proposals so their budget frees up
            for stale in (session.query(HedgeAction)
                          .filter(HedgeAction.status == "PROPOSED",
                                  HedgeAction.expires_at != None,  # noqa: E711
                                  HedgeAction.expires_at < now)
                          .all()):
                stale.status = "EXPIRED"
                log.info("%s proposal #%d expired unanswered", _PREFIX, stale.id)

            for key, grp in shorts.items():
                mtm = grp.get("mtm")
                iv = grp.get("iv_mean_pct")

                iv_series = [
                    v for (v,) in session.query(IvSnapshot.iv_pct)
                    .filter(IvSnapshot.straddle_key == key,
                            IvSnapshot.at >= now.replace(hour=0, minute=0, second=0,
                                                         microsecond=0),
                            IvSnapshot.iv_pct != None)  # noqa: E711
                    .order_by(IvSnapshot.at.desc())
                    .limit(_IV_SERIES_WINDOW).all()
                ][::-1]
                if iv is not None:
                    iv_series.append(iv)

                session.add(IvSnapshot(at=now, underlying=grp["underlying"],
                                       straddle_key=key, mtm=mtm, iv_pct=iv))

                if mtm is None:
                    continue
                if get_hedge(session, key, ("ACTIVE",)) is not None:
                    continue   # wings are on — loss is capped, alerts pause
                drawdown = evaluate_straddle(key, mtm, iv_series, settings, st, now)
                if drawdown is not None:
                    rec = st["straddles"][key]
                    tail = (
                        "Consider buying OTM wings against the shorts."
                        if mode == "ALERT" else
                        "Building wing hedge proposal — approve on /control."
                        if mode == "SEMI_AUTO" else
                        "AUTO mode: placing wings now."
                    )
                    text = (
                        f"⚠️ <b>{grp['underlying']} straddle defense</b>\n"
                        f"Drawdown ₹{drawdown:,.0f} from peak "
                        f"(peak ₹{rec['peak']:,.0f} → now ₹{mtm:,.0f}) "
                        f"with IV rising ({iv:.1f}%).\n"
                        f"Legs: {', '.join(grp['legs'])}\n"
                        f"{tail} "
                        f"Alert {rec['alerts']}/{settings.STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY} today."
                    )
                    send_telegram(settings, text)
                    log.warning("%s ALERT %s drawdown=%.0f iv=%.2f mode=%s", _PREFIX,
                                grp["underlying"], drawdown, iv or -1, mode)
                    if mode != "ALERT" and get_hedge(session, key, ("PROPOSED",)) is None:
                        action = propose_hedge(session, settings, key, "reactive",
                                               now, mode, kite=kite)
                        if action is not None and mode == "AUTO":
                            execute_hedge(session, settings, action, now)

            # once-daily pre-hedge window — the cheap moment to buy wings.
            # Only fires within 60 min of the configured time so enabling the
            # toggle late in the evening doesn't trigger a stale action.
            pre = settings.STRADDLE_DEFENSE_PREHEDGE_TIME
            pre_min = _hhmm_to_min(pre) if pre else None
            now_min = now.hour * 60 + now.minute
            in_window = pre_min is not None and pre_min <= now_min < pre_min + 60
            if shorts and in_window and not st["prehedge_sent"]:
                st["prehedge_sent"] = True
                names = ", ".join(sorted({g["underlying"] for g in shorts.values()}))
                if mode == "ALERT":
                    send_telegram(settings, (
                        f"\U0001f552 <b>Pre-hedge window</b> ({pre} IST)\n"
                        f"Short straddles open: {names}. Evening IV ramp ahead — "
                        f"wings are at their cheapest now."
                    ))
                    log.info("%s pre-hedge reminder sent (%s)", _PREFIX, names)
                else:
                    for key in shorts:
                        if get_hedge(session, key, ("PROPOSED", "ACTIVE")) is not None:
                            continue
                        action = propose_hedge(session, settings, key, "prehedge",
                                               now, mode, kite=kite)
                        if action is not None and mode == "AUTO":
                            execute_hedge(session, settings, action, now)
                    log.info("%s pre-hedge window processed (%s, mode=%s)",
                             _PREFIX, names, mode)

            # scheduled / forced wing exit after the IV cool-off
            _maybe_unwind_scheduled(session, settings, st, now, mode)

            # prune old snapshots once per day (piggybacks the first tick)
            if not st.get("pruned"):
                st["pruned"] = True
                session.query(IvSnapshot).filter(
                    IvSnapshot.at < now - timedelta(days=_SNAPSHOT_RETENTION_DAYS)
                ).delete(synchronize_session=False)

            session.commit()
            _save_state(st)
    except Exception as exc:
        log.warning("%s tick failed: %s", _PREFIX, exc)


# ── /control card data ────────────────────────────────────────────────────────

def _hedge_info(a: Any) -> dict:
    return {
        "id": a.id,
        "status": a.status,
        "trigger": a.trigger,
        "ce_symbol": a.ce_symbol,
        "pe_symbol": a.pe_symbol,
        "quantity": a.quantity,
        "est_cost": a.est_cost,
        "entry_cost": a.entry_cost,
        "confirm_token": a.confirm_token,
        "expires_at": a.expires_at,
        "dry_run": a.dry_run,
    }


def current_status(session: Any, settings: Any) -> list[dict]:
    """Latest tracked straddles for the /control defense card: one dict per
    straddle_key seen today (plus any straddle with a live hedge/proposal that
    has no snapshot yet), each carrying its hedge state."""
    from app.storage import HedgeAction, IvSnapshot

    now = datetime.now(IST)
    st = _today_state(now)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(IvSnapshot)
        .filter(IvSnapshot.at >= day_start)
        .order_by(IvSnapshot.at.desc())
        .limit(600)
        .all()
    )
    latest: dict[str, Any] = {}
    series: dict[str, list[float]] = {}
    for r in rows:                       # newest → oldest
        if r.straddle_key not in latest:
            latest[r.straddle_key] = r
        if r.iv_pct is not None:
            series.setdefault(r.straddle_key, []).append(r.iv_pct)

    hedges: dict[str, Any] = {}
    for a in (session.query(HedgeAction)
              .filter(HedgeAction.status.in_(["PROPOSED", "ACTIVE"]))
              .order_by(HedgeAction.id)
              .all()):
        hedges[a.straddle_key] = a

    out = []
    for key, r in latest.items():
        ivs = series.get(key, [])[::-1]  # oldest → newest
        rec = st["straddles"].get(key, {})
        peak = rec.get("peak")
        hedge = hedges.pop(key, None)
        out.append({
            "key": key,
            "underlying": r.underlying,
            "at": r.at,
            "mtm": r.mtm,
            "peak": peak,
            "drawdown": (peak - r.mtm) if (peak is not None and r.mtm is not None) else None,
            "iv_pct": r.iv_pct,
            "iv_rising": iv_rising(ivs, settings.STRADDLE_DEFENSE_IV_SAMPLES),
            "alerts": rec.get("alerts", 0),
            "hedge": _hedge_info(hedge) if hedge is not None else None,
        })
    # hedges whose straddle has no snapshot today (e.g. monitor just enabled)
    for key, a in hedges.items():
        out.append({
            "key": key, "underlying": a.underlying, "at": a.proposed_at,
            "mtm": None, "peak": None, "drawdown": None, "iv_pct": None,
            "iv_rising": False, "alerts": 0, "hedge": _hedge_info(a),
        })
    out.sort(key=lambda d: d["underlying"])
    return out
