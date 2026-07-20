"""Scheduled short-straddle jobs: entry at configured times + squareoff at 23:20 IST.

Entry flow:
  1. validate_straddle() → picks ATM CE + PE symbols, checks margin/spread
  2. Creates an Alert row (strategy_id="sched_straddle_<UNDERLYING>")
  3. Calls _process_straddle() directly (same path as voice straddle)

Squareoff flow:
  Closes all open MCX CE/PE positions for CRUDEOILM and NATURALGAS.
  Reuses the eod_squareoff_job cancellation + square_off pattern verbatim.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

_SQUAREOFF_UNDERLYINGS = {"NATURALGAS"}
_SQUAREOFF_REASON = "scheduled_straddle_squareoff"


# ── ADX gate ─────────────────────────────────────────────────────────────────

def _fetch_adx(underlying: str, settings: Any, kite: Any, session: Any) -> float | None:
    """Return ADX(period) on ADX_CANDLE_INTERVAL candles for the front-month futures.

    Returns None on any failure so the caller can decide whether to proceed.
    """
    from app.adx import compute_adx
    from app.storage import Instrument
    from app.config import IST

    period    = settings.ADX_PERIOD
    interval  = settings.ADX_CANDLE_INTERVAL
    interval_mins = int(interval.replace("minute", "").replace("min", "")) if "min" in interval else 1
    n_candles = period * 3 + 5  # generous buffer; 2*period+1 required minimum

    futures = (
        session.query(Instrument)
        .filter(
            Instrument.name == underlying,
            Instrument.instrument_type == "FUT",
            Instrument.exchange == "MCX",
            Instrument.expiry != None,  # noqa: E711
        )
        .order_by(Instrument.expiry.asc())
        .first()
    )
    if futures is None:
        log.warning("[sched_straddle] ADX: no futures instrument found for %s", underlying)
        return None

    now_ist = datetime.now(IST)
    from_dt = now_ist - timedelta(minutes=n_candles * interval_mins + 60)
    try:
        raw = kite.historical_data(futures.instrument_token, from_dt, now_ist, interval)
    except Exception as exc:
        log.warning("[sched_straddle] ADX: historical_data failed for %s: %s", underlying, exc)
        return None

    if not raw:
        log.warning("[sched_straddle] ADX: empty candles for %s", underlying)
        return None

    adx = compute_adx(raw, period=period)
    log.info(
        "[sched_straddle] ADX(%d) %s on %s = %.2f",
        period, underlying, interval, adx if adx is not None else float("nan"),
    )
    return adx


# ── Entry job ─────────────────────────────────────────────────────────────────

def run_scheduled_straddle(
    underlying: str,
    exchange: str,
    qty: int,
    settings: Any,
    session_factory: Any,
    adx_threshold: float | None = None,
) -> None:
    """Place a scheduled short straddle for `underlying`.

    If adx_threshold is given, fetches ADX first and skips if ADX >= threshold.
    """
    import app.state as state
    from app.kite_session import get_session_manager
    from app.config import IST
    from app.storage import Alert
    from app.voice.straddle import validate_straddle

    if state.get_session_invalid():
        log.warning("[sched_straddle] %s: session invalid — skipping", underlying)
        return

    if state.is_emergency_stop():
        log.warning("[sched_straddle] %s: emergency stop active — skipping", underlying)
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[sched_straddle] %s: cannot get kite client: %s", underlying, exc)
        return

    now = datetime.now(IST)

    with session_factory() as session:
        # ADX gate — inside session so _fetch_adx can query the instruments table
        if adx_threshold is not None:
            adx = _fetch_adx(underlying, settings, kite, session)
            if adx is None:
                log.warning(
                    "[sched_straddle] %s: ADX unavailable — skipping to avoid unvalidated entry",
                    underlying,
                )
                return
            if adx >= adx_threshold:
                log.info(
                    "[sched_straddle] %s: ADX %.2f >= threshold %.2f — skipping (trending market)",
                    underlying, adx, adx_threshold,
                )
                return

        # validate_straddle picks the ATM CE + PE symbols and checks margin/spread
        try:
            sv = validate_straddle(
                underlying, qty, kite, session, settings, now, exchange=exchange,
            )
        except Exception as exc:
            log.error("[sched_straddle] %s: validate_straddle failed: %s", underlying, exc)
            return

        if not sv.margin_ok:
            log.error(
                "[sched_straddle] %s: margin insufficient (%s) — skipping",
                underlying, sv.block_reason,
            )
            return

        if not sv.spread_ok:
            log.warning(
                "[sched_straddle] %s: spread too wide (%s) — skipping",
                underlying, sv.block_reason,
            )
            return

        ikey = str(uuid.uuid4())
        alert = Alert(
            received_at=now,
            strategy_id=f"sched_straddle_{underlying}",
            tv_ticker=underlying,
            tv_exchange=exchange,
            action="STRADDLE_SHORT",
            order_type=None,
            entry_price=0.0,
            stop_loss=None,
            sl_percent=None,
            atr=None,
            quantity_hint=qty,
            product="NRML",
            tv_time=now.isoformat(),
            bar_time=None,
            interval="scheduled",
            idempotency_key=ikey,
            raw_payload="{}",
            processed=False,
        )
        session.add(alert)
        session.commit()
        session.refresh(alert)

        entry_data = {
            "underlying"            : underlying,
            "exchange"              : exchange,
            "quantity"              : qty,
            "_straddle_ce_symbol"   : sv.ce.tradingsymbol,
            "_straddle_pe_symbol"   : sv.pe.tradingsymbol,
            "_straddle_atm_strike"  : sv.atm_strike,
            "_straddle_expiry"      : str(sv.expiry),
            "_straddle_lot_size"    : sv.lot_size,
            "_straddle_lot_units"   : sv.lot_units,
            "_straddle_net_credit"  : sv.net_credit_per_lot,
            "_straddle_est_sl"      : sv.estimated_sl_per_lot,
            "_straddle_all_ok"      : sv.all_ok,
            "_straddle_force_spread": False,
        }

    log.info(
        "[sched_straddle] %s: queuing straddle alert=%d CE=%s PE=%s qty=%d",
        underlying, alert.id, sv.ce.tradingsymbol, sv.pe.tradingsymbol, qty,
    )

    # Import here to avoid circular dep (main imports scheduler imports this)
    from app.main import _process_straddle
    _process_straddle(alert.id, entry_data, settings)


# ── Squareoff job ─────────────────────────────────────────────────────────────

def squareoff_scheduled_straddles(settings: Any, session_factory: Any) -> None:
    """Buy back all open MCX CE/PE positions for CRUDEOILM and NATURALGAS.

    Mirrors eod_squareoff_job logic exactly; only the position filter differs.
    """
    import app.state as state
    from app.kite_session import get_session_manager
    from app.storage import ClosedTrade, Gtt, Instrument, Order, Position
    from app.orders import cancel_gtt, square_off
    from app.config import IST

    if state.get_session_invalid():
        log.warning("[sched_straddle] squareoff: session invalid — skipping")
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[sched_straddle] squareoff: cannot get kite client: %s", exc)
        return

    now = datetime.now(IST)

    with session_factory() as session:
        # Only target positions whose entry Order has a straddle_id — leaves
        # TV-strategy single legs (and any other non-straddle MCX positions)
        # for the EOD MCX squareoff job to handle.
        open_positions = (
            session.query(Position)
            .join(Order, Order.id == Position.order_id)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.exchange == "MCX",
                Position.underlying.in_(_SQUAREOFF_UNDERLYINGS),
                Position.instrument_type.in_(["CE", "PE"]),
                Order.straddle_id.isnot(None),
                ClosedTrade.id == None,  # noqa: E711
                # Paper straddles are closed at LTP by the paper monitor.
                Order.dry_run == False,  # noqa: E712
            )
            .all()
        )

        if not open_positions:
            log.info("[sched_straddle] squareoff: no open straddle positions — nothing to do")
            return

        log.info(
            "[sched_straddle] squareoff: closing %d position(s): %s",
            len(open_positions),
            [p.tradingsymbol for p in open_positions],
        )

        for position in open_positions:
            try:
                # Cancel GTT
                gtt = (
                    session.query(Gtt)
                    .filter(Gtt.order_id == position.order_id, Gtt.status == "ACTIVE")
                    .first()
                )
                if gtt and gtt.kite_gtt_id:
                    try:
                        cancel_gtt(kite, gtt.kite_gtt_id)
                    except Exception as exc:
                        log.warning(
                            "[sched_straddle] squareoff: cancel GTT %d failed: %s",
                            gtt.kite_gtt_id, exc,
                        )
                    gtt.status = "CANCELLED"
                    gtt.updated_at = now

                # Unregister trailing SL if active
                try:
                    from app.main import _trailing_manager
                    if _trailing_manager is not None:
                        _trailing_manager.unregister(position.tradingsymbol)
                except Exception:
                    pass

                entry_order = (
                    session.query(Order).filter(Order.id == position.order_id).first()
                )
                instrument = (
                    session.query(Instrument)
                    .filter(
                        Instrument.tradingsymbol == position.tradingsymbol,
                        Instrument.exchange == position.exchange,
                    )
                    .first()
                )
                if instrument is None:
                    log.error(
                        "[sched_straddle] squareoff: instrument not found for %s — skipping",
                        position.tradingsymbol,
                    )
                    continue

                product    = entry_order.product if entry_order else "NRML"
                entry_side = entry_order.transaction_type if entry_order else "SELL"

                sq_id = square_off(kite, instrument, position.quantity, product, entry_side)

                from app.storage import booked_partial_pnl, trade_meta_for_order
                _sq_sid, _sq_dry = trade_meta_for_order(session, entry_order)
                ct = ClosedTrade(
                    position_id=position.id,
                    exchange=position.exchange,
                    tradingsymbol=position.tradingsymbol,
                    entry_premium=position.entry_premium,
                    exit_premium=0.0,
                    pnl=booked_partial_pnl(position),
                    exit_reason=_SQUAREOFF_REASON,
                    opened_at=position.opened_at,
                    closed_at=now,
                    strategy_id=_sq_sid,
                    dry_run=_sq_dry,
                )
                session.add(ct)
                log.info(
                    "[sched_straddle] squareoff: %s qty=%d sq_order=%s",
                    position.tradingsymbol, position.quantity, sq_id,
                )
            except Exception as exc:
                log.error(
                    "[sched_straddle] squareoff: failed for %s: %s",
                    position.tradingsymbol, exc, exc_info=True,
                )

        session.commit()


