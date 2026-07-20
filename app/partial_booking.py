"""Partial profit booking — bank part of a winner, ride the rest risk-free.

Once a position has travelled ``PARTIAL_BOOK_TRIGGER_PCT`` of the way from its
entry to its GTT target, ``PARTIAL_BOOK_QTY_PCT`` of the open quantity is closed
and the GTT is re-armed on the remainder — at breakeven when
``PARTIAL_BOOK_MOVE_SL_TO_BREAKEVEN`` is set, so the surviving slice cannot turn
into a loss.

Ordering is deliberate: the GTT is resized to the remainder *before* the
reducing order goes out, so the position is never bigger than its protection.
If the reducing order then fails, the GTT is restored to full size.

Each position is booked at most once — ``partial_booked_qty`` is the latch. The
realized amount is banked on the Position rather than written as its own
ClosedTrade, because ``closed_trades.position_id`` is unique; every ClosedTrade
site folds it back in via ``storage.booked_partial_pnl``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.config import IST
from app.kite_session import get_session_manager
from app.storage import ClosedTrade, Gtt, Instrument, Order, Position

log = logging.getLogger(__name__)

_PREFIX = "[partial_book]"


def progress_to_target(entry_side: str, entry: float, target: float, ltp: float) -> float:
    """How far the price has travelled from entry toward target, as a fraction.

    0.0 = still at entry, 1.0 = target reached. Negative when the position is
    under water. Returns 0.0 when entry and target coincide.
    """
    if entry_side.upper() == "SELL":
        span = entry - target          # short: profit as the premium falls
        moved = entry - ltp
    else:
        span = target - entry          # long: profit as the premium rises
        moved = ltp - entry
    if span <= 0:
        return 0.0
    return moved / span


def qty_to_book(open_qty: int, pct: float) -> int:
    """Slice to close, leaving at least one unit running. 0 = cannot split."""
    if open_qty < 2:
        return 0
    booked = int(open_qty * pct)
    return max(1, min(booked, open_qty - 1))


def _eligible_rows(session: Any, paper: bool) -> list[tuple[Position, Order, Gtt, Instrument]]:
    """Open, not-yet-booked positions in the active mode that still hold a GTT."""
    rows = (
        session.query(Position, Order, Gtt, Instrument)
        .join(Order, Position.order_id == Order.id)
        .join(Gtt, Position.gtt_id == Gtt.id)
        .join(
            Instrument,
            (Instrument.tradingsymbol == Position.tradingsymbol)
            & (Instrument.exchange == Position.exchange),
        )
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(
            ClosedTrade.id == None,                         # noqa: E711 — still open
            Order.dry_run == paper,                         # never cross paper/live
            Position.partial_booked_qty == 0,               # book once per position
            Position.quantity > 1,
            Gtt.status.in_(["ACTIVE", "DRY_RUN"]),
        )
        .all()
    )
    return [(p, o, g, i) for p, o, g, i in rows]


def book_partial(
    session: Any,
    settings: Any,
    position: Position,
    order: Order,
    gtt: Gtt,
    instrument: Instrument,
    ltp: float,
    now: datetime,
    kite: Any,
    paper: bool,
    trailing_manager: Any = None,
) -> bool:
    """Close part of one position and re-arm the GTT on the remainder.

    Returns True when the book completed. Caller commits.
    """
    from app.orders import compute_oco_limits, modify_gtt, place_entry
    from app.paper_trading import paper_pnl   # P&L math incl. MCX lot units
    from app.symbol_mapper import round_to_tick

    entry_side = (order.transaction_type or "BUY").upper()
    open_qty = position.quantity
    book_qty = qty_to_book(open_qty, settings.PARTIAL_BOOK_QTY_PCT)
    if book_qty <= 0:
        return False
    remaining = open_qty - book_qty

    tick = Decimal(str(instrument.tick_size or 0.01))
    if settings.PARTIAL_BOOK_MOVE_SL_TO_BREAKEVEN:
        new_sl = float(round_to_tick(Decimal(str(position.entry_premium)), tick))
    else:
        new_sl = gtt.sl_trigger
    target = gtt.target_trigger

    # The re-armed band must still bracket the current price, or the broker
    # rejects it ("trigger already met") and the remainder ends up naked.
    if entry_side == "SELL":
        band_ok = target < ltp < new_sl
    else:
        band_ok = new_sl < ltp < target
    if not band_ok:
        log.info(
            "%s %s: breakeven band [%.4f-%.4f] does not bracket LTP %.4f — skipping",
            _PREFIX, position.tradingsymbol, min(new_sl, target), max(new_sl, target), ltp,
        )
        return False

    sl_limit_d, tgt_limit_d = compute_oco_limits(
        new_sl, target, entry_side, float(settings.OCO_SLIPPAGE_BUFFER_PCT),
    )
    sl_limit = float(round_to_tick(Decimal(str(sl_limit_d)), tick))
    tgt_limit = float(round_to_tick(Decimal(str(tgt_limit_d)), tick))

    gtt_resized = False
    if not paper and gtt.kite_gtt_id is not None:
        try:
            modify_gtt(
                kite,
                gtt.kite_gtt_id,
                sl_trigger=new_sl,
                sl_limit=sl_limit,
                target_trigger=target,
                target_limit=tgt_limit,
                last_price=ltp,
                instrument=instrument,
                qty=remaining,
                product=order.product,
                entry_side=entry_side,
            )
            gtt_resized = True
        except Exception as exc:
            log.error("%s %s: GTT resize failed (%s) — not booking", _PREFIX,
                      position.tradingsymbol, exc)
            return False

    exit_order_id = None
    if not paper:
        # Deliberately not orders.square_off(): its 30s per-symbol dedup would
        # swallow a genuine full exit arriving right after this partial.
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        try:
            exit_order_id = place_entry(
                kite, instrument, exit_side, book_qty, "MARKET", order.product,
            )
        except Exception as exc:
            log.error("%s %s: reducing order failed (%s) — restoring full-size GTT",
                      _PREFIX, position.tradingsymbol, exc)
            if gtt_resized:
                try:
                    modify_gtt(
                        kite,
                        gtt.kite_gtt_id,
                        sl_trigger=gtt.sl_trigger,
                        sl_limit=gtt.sl_order_price,
                        target_trigger=gtt.target_trigger,
                        target_limit=gtt.target_order_price,
                        last_price=ltp,
                        instrument=instrument,
                        qty=open_qty,
                        product=order.product,
                        entry_side=entry_side,
                    )
                except Exception as rexc:
                    log.error(
                        "%s %s: CRITICAL — GTT left at qty=%d while position holds %d: %s"
                        " — MANUAL INTERVENTION REQUIRED",
                        _PREFIX, position.tradingsymbol, remaining, open_qty, rexc,
                    )
            return False

    booked_pnl = paper_pnl(
        entry_side, position.entry_premium, ltp, book_qty,
        position.exchange, position.underlying or position.tradingsymbol, settings,
    )

    position.quantity = remaining
    position.partial_booked_qty = book_qty
    position.partial_booked_pnl = round(booked_pnl, 2)
    position.partial_booked_at = now
    position.current_sl = new_sl
    position.last_updated_at = now
    gtt.sl_trigger = new_sl
    gtt.sl_order_price = sl_limit
    gtt.target_order_price = tgt_limit
    gtt.modification_count += 1
    gtt.updated_at = now

    # Trailing state is keyed by qty and initial SL — re-register the remainder.
    if trailing_manager is not None and not paper and gtt.kite_gtt_id is not None:
        try:
            trailing_manager.unregister(instrument.instrument_token)
            from app import state as _state
            if _state.is_trailing_enabled():
                trailing_manager.register(
                    tradingsymbol=position.tradingsymbol,
                    instrument_token=instrument.instrument_token,
                    exchange=position.exchange,
                    entry_side=entry_side,
                    sl_pct=_state.get_sl_pct(settings.SL_PREMIUM_PCT),
                    qty=remaining,
                    product=order.product,
                    target_price=target,
                    initial_sl=new_sl,
                    fill_price=ltp,
                    gtt_db_id=gtt.id,
                    kite_gtt_id=gtt.kite_gtt_id,
                    tick_size=float(instrument.tick_size or 0.01),
                )
        except Exception as exc:
            log.warning("%s %s: trailing re-register failed: %s", _PREFIX,
                        position.tradingsymbol, exc)

    log.info(
        "%s%s booked %d of %d %s @ %.4f pnl=%.2f — %d left, SL %.4f%s (order=%s)",
        _PREFIX, " [paper]" if paper else "", book_qty, open_qty,
        position.tradingsymbol, ltp, booked_pnl, remaining, new_sl,
        " (breakeven)" if settings.PARTIAL_BOOK_MOVE_SL_TO_BREAKEVEN else "",
        exit_order_id,
    )
    return True


def partial_booking_job(settings: Any, session_factory: Any) -> None:
    """1-min cron: bank partial profits on positions that have run far enough."""
    from app import state

    if not state.is_partial_booking_enabled(settings.PARTIAL_BOOKING_ENABLED):
        return

    paper = state.is_paper_mode(settings.DRY_RUN)
    trigger = float(settings.PARTIAL_BOOK_TRIGGER_PCT)

    with session_factory() as session:
        rows = _eligible_rows(session, paper)
        if not rows:
            return

        try:
            kite = get_session_manager().get_kite()
        except Exception as exc:
            log.debug("%s skipped — no Kite session for quotes: %s", _PREFIX, exc)
            return

        keys = sorted({f"{p.exchange}:{p.tradingsymbol}" for p, _, _, _ in rows})
        try:
            quotes = kite.ltp(keys)
        except Exception as exc:
            log.warning("%s LTP fetch failed for %d symbols: %s", _PREFIX, len(keys), exc)
            return

        now = datetime.now(IST)
        trailing_manager = _trailing_manager()
        booked_any = False

        for position, order, gtt, instrument in rows:
            key = f"{position.exchange}:{position.tradingsymbol}"
            raw = quotes.get(key, {}).get("last_price")
            if raw is None:
                continue
            ltp = float(raw)
            entry_side = (order.transaction_type or "BUY").upper()
            moved = progress_to_target(
                entry_side, position.entry_premium, gtt.target_trigger, ltp,
            )
            if moved < trigger:
                continue
            if book_partial(
                session, settings, position, order, gtt, instrument,
                ltp, now, kite, paper, trailing_manager,
            ):
                booked_any = True

        if booked_any:
            session.commit()


def _trailing_manager() -> Any:
    """The live TrailingSlManager, or None when the app is not fully started.

    Imported lazily: app.main imports the scheduler, which imports this module.
    """
    try:
        import app.main as _main
        return getattr(_main, "_trailing_manager", None)
    except Exception:
        return None
