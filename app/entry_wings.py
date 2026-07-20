"""Defined-risk straddles — buy the protective wings at entry, not after.

The straddle-defense monitor in ``app.straddle_defense`` reacts: it waits for a
drawdown and an IV expansion before proposing wings. This module removes the
wait. When ``STRADDLE_ENTRY_WINGS_ENABLED`` is on, a long CE and a long PE
``STRADDLE_ENTRY_WING_STEPS`` strikes outside the short strikes are bought
*before* the short legs go out, so maximum loss is capped from the first tick
and the position is an iron butterfly rather than a naked straddle.

Ordering is the whole point: protection first, exposure second. If the wings
cannot be bought and ``STRADDLE_ENTRY_WINGS_REQUIRED`` is set, the straddle is
abandoned before any risk exists. If the short legs then fail, the wings are
sold back — the caller does that through :func:`reverse_entry_wings`.

The placed pair is recorded as a ``HedgeAction`` with ``trigger="entry"`` so the
defense monitor sees the straddle is already hedged and will not buy a second
set of wings, and so the timed unwinds leave it alone — entry wings stay on
until the straddle itself is squared off.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

_PREFIX = "[entry_wings]"

#: HedgeAction.trigger value marking a hedge that was bought with the straddle.
ENTRY_TRIGGER = "entry"


def plan_entry_wings(
    session: Any,
    settings: Any,
    *,
    underlying: str,
    exchange: str,
    ce_short_symbol: str,
    pe_short_symbol: str,
    quantity: int,
    kite: Any,
) -> dict | None:
    """Choose and price the wing pair for a straddle about to be entered.

    Returns ``{ce, pe, cost, credit, cost_pct}`` or None when no usable wings
    exist. Pricing failures leave ``cost`` at 0.0 so the caller can still place
    (the cost cap is only enforced when a real quote came back).
    """
    from app.storage import Instrument
    from app.straddle_defense import _chain_interval, _find_wing, _units_multiplier, _wing_prices

    def _inst(sym: str) -> Any:
        return (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == sym, Instrument.exchange == exchange)
            .first()
        )

    ce_short, pe_short = _inst(ce_short_symbol), _inst(pe_short_symbol)
    if ce_short is None or pe_short is None or not ce_short.expiry:
        log.warning("%s %s: short-leg instruments not found (%s / %s)",
                    _PREFIX, underlying, ce_short_symbol, pe_short_symbol)
        return None

    interval = _chain_interval(session, ce_short.name, ce_short.expiry, exchange, settings)
    steps = max(int(settings.STRADDLE_ENTRY_WING_STEPS), 1)
    ce_wing = _find_wing(session, ce_short.name, ce_short.expiry, exchange, "CE",
                         float(ce_short.strike), interval, steps)
    pe_wing = _find_wing(session, pe_short.name, pe_short.expiry, exchange, "PE",
                         float(pe_short.strike), interval, steps)
    if ce_wing is None or pe_wing is None:
        log.warning("%s %s: no wing strikes %d intervals (%.2f) outside CE %.2f / PE %.2f",
                    _PREFIX, underlying, steps, interval, ce_short.strike, pe_short.strike)
        return None

    units = _units_multiplier(settings, exchange, underlying)
    cost = credit = 0.0
    if kite is not None:
        ce_px, pe_px = _wing_prices(kite, exchange, ce_wing.tradingsymbol, pe_wing.tradingsymbol)
        cost = (ce_px + pe_px) * quantity * units
        short_ce_px, short_pe_px = _wing_prices(kite, exchange, ce_short_symbol, pe_short_symbol)
        credit = (short_ce_px + short_pe_px) * quantity * units

    cost_pct = (cost / credit) if credit > 0 else 0.0
    return {
        "ce": ce_wing,
        "pe": pe_wing,
        "underlying": underlying,
        "exchange": exchange,
        "quantity": quantity,
        "cost": round(cost, 2),
        "credit": round(credit, 2),
        "cost_pct": cost_pct,
    }


def wings_too_expensive(settings: Any, plan: dict) -> bool:
    """True when priced wings would eat more of the credit than allowed."""
    cap = float(settings.STRADDLE_ENTRY_WING_MAX_COST_PCT)
    return cap > 0 and plan["credit"] > 0 and plan["cost_pct"] > cap


def place_entry_wings(settings: Any, plan: dict, kite: Any, product: str) -> dict:
    """Buy both wings concurrently. Returns {ce_order_id, pe_order_id, error}.

    A half-filled pair is reversed here — a lone wing is not protection, and
    leaving it on would quietly change the strategy's risk profile.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.orders import place_entry, square_off

    qty = plan["quantity"]
    ce_oid = pe_oid = None
    ce_err = pe_err = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_ce = pool.submit(place_entry, kite, plan["ce"], "BUY", qty, "MARKET", product)
        fut_pe = pool.submit(place_entry, kite, plan["pe"], "BUY", qty, "MARKET", product)
        try:
            ce_oid = fut_ce.result(timeout=settings.STRADDLE_FILL_TIMEOUT_SECS)
        except Exception as exc:
            ce_err = exc
        try:
            pe_oid = fut_pe.result(timeout=settings.STRADDLE_FILL_TIMEOUT_SECS)
        except Exception as exc:
            pe_err = exc

    if bool(ce_oid) != bool(pe_oid):
        lone, label = (plan["ce"], "CE") if ce_oid else (plan["pe"], "PE")
        log.error("%s %s: %s wing failed (%s) — reversing the %s wing",
                  _PREFIX, plan["underlying"], "PE" if ce_oid else "CE",
                  pe_err if ce_oid else ce_err, label)
        try:
            square_off(kite, lone, qty, product, entry_side="BUY")
        except Exception as exc:
            log.error("%s %s: CRITICAL — cannot reverse lone %s wing: %s "
                      "— MANUAL INTERVENTION REQUIRED",
                      _PREFIX, plan["underlying"], label, exc)
        return {"ce_order_id": None, "pe_order_id": None, "error": ce_err or pe_err}

    if not ce_oid and not pe_oid:
        return {"ce_order_id": None, "pe_order_id": None, "error": ce_err or pe_err}

    return {"ce_order_id": ce_oid, "pe_order_id": pe_oid, "error": None}


def reverse_entry_wings(plan: dict, kite: Any, product: str) -> None:
    """Sell the wings back — used when the short legs failed after wings filled."""
    from app.orders import square_off

    for leg, label in ((plan["ce"], "CE"), (plan["pe"], "PE")):
        try:
            square_off(kite, leg, plan["quantity"], product, entry_side="BUY")
        except Exception as exc:
            log.error("%s %s: CRITICAL — cannot reverse %s wing after straddle "
                      "failure: %s — MANUAL INTERVENTION REQUIRED",
                      _PREFIX, plan["underlying"], label, exc)


def record_entry_wings(
    session: Any,
    settings: Any,
    plan: dict,
    orders: dict,
    *,
    straddle_key: str,
    now: datetime,
    dry: bool,
    product: str,
) -> Any:
    """Persist the wing pair as Order/Position rows plus an ACTIVE HedgeAction.

    The HedgeAction is what stops ``straddle_defense`` proposing a second hedge
    for the same straddle; ``trigger=entry`` keeps the timed unwinds off it.
    """
    from app.storage import Alert, HedgeAction, Order, Position

    alert = Alert(
        received_at=now,
        strategy_id=f"entry_wings_{plan['underlying']}",
        tv_ticker=plan["underlying"],
        tv_exchange=plan["exchange"],
        action="HEDGE",
        order_type="MARKET",
        product=product,
        idempotency_key=f"ew-{uuid.uuid4()}",
        raw_payload=json.dumps({
            "straddle_key": straddle_key,
            "ce": plan["ce"].tradingsymbol,
            "pe": plan["pe"].tradingsymbol,
            "qty": plan["quantity"],
            "est_cost": plan["cost"],
        }),
        processed=True,
    )
    session.add(alert)
    session.flush()

    for inst, kite_oid in ((plan["ce"], orders.get("ce_order_id")),
                           (plan["pe"], orders.get("pe_order_id"))):
        o = Order(
            alert_id=alert.id,
            kite_order_id=kite_oid,
            variety="regular",
            exchange=plan["exchange"],
            tradingsymbol=inst.tradingsymbol,
            transaction_type="BUY",
            order_type="MARKET",
            product=product,
            quantity=plan["quantity"],
            status="DRY_RUN" if dry else "COMPLETE",
            placed_at=now,
            updated_at=now,
            dry_run=dry,
            straddle_id=straddle_key,
        )
        session.add(o)
        session.flush()
        # Wings carry no GTT: their loss is bounded by the premium paid, and the
        # EOD squareoff closes them alongside the straddle.
        session.add(Position(
            order_id=o.id,
            exchange=plan["exchange"],
            tradingsymbol=inst.tradingsymbol,
            underlying=plan["underlying"],
            instrument_type=inst.instrument_type,
            entry_premium=0.0,
            current_sl=0.0,
            quantity=plan["quantity"],
            lot_size=inst.lot_size,
            opened_at=now,
            last_updated_at=now,
        ))

    action = HedgeAction(
        straddle_key=straddle_key,
        underlying=plan["underlying"],
        exchange=plan["exchange"],
        mode="AUTO",
        trigger=ENTRY_TRIGGER,
        status="ACTIVE",
        ce_symbol=plan["ce"].tradingsymbol,
        pe_symbol=plan["pe"].tradingsymbol,
        quantity=plan["quantity"],
        est_cost=plan["cost"],
        entry_cost=plan["cost"] or None,
        ce_order_id=orders.get("ce_order_id"),
        pe_order_id=orders.get("pe_order_id"),
        proposed_at=now,
        executed_at=now,
        dry_run=dry,
    )
    session.add(action)
    return action


def attach_entry_wings(
    session: Any,
    settings: Any,
    *,
    underlying: str,
    exchange: str,
    ce_short_symbol: str,
    pe_short_symbol: str,
    quantity: int,
    product: str,
    kite: Any,
    dry: bool,
) -> tuple[dict | None, str | None]:
    """Plan and buy the wings for a straddle about to be placed.

    Returns ``(plan, blocking_reason)``. ``plan`` is non-None once the wings are
    on (or simulated in paper mode) and should be passed to
    :func:`record_entry_wings` after the short legs fill, or to
    :func:`reverse_entry_wings` if they do not. ``blocking_reason`` is set only
    when ``STRADDLE_ENTRY_WINGS_REQUIRED`` makes a wing failure fatal, in which
    case the caller must abandon the straddle.
    """
    required = bool(settings.STRADDLE_ENTRY_WINGS_REQUIRED)

    def _give_up(reason: str) -> tuple[None, str | None]:
        log.warning("%s %s: %s — %s", _PREFIX, underlying, reason,
                    "abandoning straddle" if required else "entering without wings")
        return None, (reason if required else None)

    plan = plan_entry_wings(
        session, settings, underlying=underlying, exchange=exchange,
        ce_short_symbol=ce_short_symbol, pe_short_symbol=pe_short_symbol,
        quantity=quantity, kite=kite,
    )
    if plan is None:
        return _give_up("no wing strikes available")

    if wings_too_expensive(settings, plan):
        return _give_up(
            f"wings cost ₹{plan['cost']:,.0f} = {plan['cost_pct'] * 100:.0f}% of "
            f"₹{plan['credit']:,.0f} credit (cap "
            f"{settings.STRADDLE_ENTRY_WING_MAX_COST_PCT * 100:.0f}%)"
        )

    if dry:
        log.info("%s %s DRY_RUN — would BUY %s + %s qty=%d cost %.0f",
                 _PREFIX, underlying, plan["ce"].tradingsymbol,
                 plan["pe"].tradingsymbol, quantity, plan["cost"])
        return plan, None

    orders = place_entry_wings(settings, plan, kite, product)
    if orders["error"] is not None or not orders["ce_order_id"]:
        return _give_up(f"wing placement failed ({orders['error']})")

    plan["orders"] = orders
    log.info("%s %s: wings ON — BUY %s + %s qty=%d cost ₹%.0f (%.0f%% of credit)",
             _PREFIX, underlying, plan["ce"].tradingsymbol, plan["pe"].tradingsymbol,
             quantity, plan["cost"], plan["cost_pct"] * 100)
    return plan, None
