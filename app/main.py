from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import traceback

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone  # timezone used in _auth_guard
from decimal import Decimal
from math import floor
from typing import Any

from cachetools import TTLCache
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.risk as risk
import app.state as state
from app.auth import IPBlockedError, RateLimitError, check_ip, check_rate_limit
from app.config import IST, Settings, get_settings
from app.expiry_resolver import NoEligibleExpiryError, resolve_expiry
from app.kite_session import get_session_manager
from app.orders import cancel_gtt, modify_gtt, place_entry, place_gtt_oco, square_off
from app.scheduler import daily_session_check, get_last_checked_at, make_scheduler, refresh_instruments_job
from app.storage import (
    Alert, AppError, ClosedTrade, Gtt, Instrument, Order, Position,
    WebSession, _register_factory, init_db,
)
from app.strike_selector import NoValidStrikeError, select_strike
from app.symbol_mapper import _MCX_COMMODITY_NAMES as _MCX_NAMES, resolve_underlying
from app.schemas import AlertPayload
from app.watcher import EntryFilledEvent, GttFilledEvent, OrderWatcher

log = logging.getLogger(__name__)


def _dry_run(settings: Settings) -> bool:
    """Effective dry-run flag: state override (paper-mode toggle) beats .env."""
    return state.is_paper_mode(settings.DRY_RUN)


def _parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


# ── Module-level singletons ────────────────────────────────────────────────────
_SessionFactory: Any = None
_watcher: OrderWatcher | None = None
_trailing_manager: Any = None  # TrailingSlManager; typed as Any to avoid circular-import annotation
_idempotency_cache: TTLCache = TTLCache(maxsize=10_000, ttl=86_400)

# Keyed by kite_order_id; carries sl/target distances for the EntryFilledEvent callback.
_pending_order_meta: dict[str, dict] = {}

_scheduler: Any = None


# ── Risk guard helpers ────────────────────────────────────────────────────────

def _realised_loss_today(session: Any) -> float:
    """Return the total realised loss (positive number) from ClosedTrade rows closed today IST."""
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(ClosedTrade.pnl)
        .filter(ClosedTrade.closed_at >= today_start, ClosedTrade.pnl < 0)
        .all()
    )
    return abs(sum(r[0] for r in rows))


def _open_position_count(session: Any) -> int:
    return (
        session.query(Position)
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(ClosedTrade.id == None)  # noqa: E711
        .count()
    )


def _trades_today_count(session: Any) -> int:
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        session.query(Alert)
        .filter(
            Alert.action.in_(["BUY", "SELL"]),
            Alert.processed == True,  # noqa: E712
            Alert.received_at >= today_start,
        )
        .count()
    )


def _consecutive_losses(session: Any) -> int:
    rows = (
        session.query(ClosedTrade.exit_reason)
        .order_by(ClosedTrade.closed_at.desc())
        .all()
    )
    count = 0
    for (reason,) in rows:
        if reason == "SL_HIT":
            count += 1
        else:
            break
    return count


def _check_risk_guards(session: Any, settings: Settings) -> tuple[bool, str]:
    if state.is_emergency_stop():
        return False, "EMERGENCY STOP is active"
    loss = _realised_loss_today(session)
    effective_loss_cap = state.get_max_daily_loss(settings.MAX_DAILY_LOSS_ABS)
    if loss >= effective_loss_cap:
        return False, f"daily loss ₹{loss:.2f} >= limit ₹{effective_loss_cap:.2f}"
    trades = _trades_today_count(session)
    eff_max_trades = state.get_max_trades_per_day(settings.MAX_TRADES_PER_DAY)
    if trades >= eff_max_trades:
        return False, f"trades today {trades} >= MAX_TRADES_PER_DAY {eff_max_trades}"
    positions = _open_position_count(session)
    eff_max_positions = state.get_max_open_positions(settings.MAX_OPEN_POSITIONS)
    if positions >= eff_max_positions:
        return False, f"open positions {positions} >= MAX_OPEN_POSITIONS {eff_max_positions}"
    losses = _consecutive_losses(session)
    eff_consec_limit = state.get_consecutive_losses_limit(settings.CONSECUTIVE_LOSSES_LIMIT)
    if losses >= eff_consec_limit:
        return False, f"consecutive losses {losses} >= CONSECUTIVE_LOSSES_LIMIT {eff_consec_limit}"
    return True, ""


def _round_to_tick(price: float, tick: float) -> float:
    """Round price to nearest valid tick boundary (handles MCX 0.05/0.10 ticks)."""
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 2)


# ── EntryFilledEvent callback ─────────────────────────────────────────────────

def _on_entry_filled(event: EntryFilledEvent) -> None:
    """Consume a fill event: persist Position, place GTT OCO."""
    if _SessionFactory is None:
        log.error("_on_entry_filled fired but _SessionFactory is None")
        return
    settings = get_settings()
    with _SessionFactory() as session:
        order = (
            session.query(Order)
            .filter(Order.kite_order_id == event.kite_order_id)
            .first()
        )
        if order is None:
            log.error("EntryFilledEvent: no Order row for kite_order_id=%s", event.kite_order_id)
            return

        now = datetime.now(IST)
        order.status = "COMPLETE"
        order.fill_price = event.fill_price
        order.fill_qty = event.fill_qty
        order.updated_at = now

        meta = _pending_order_meta.pop(event.kite_order_id, {})
        instrument_type = meta.get("instrument_type", "CE")
        entry_side = meta.get("entry_side", "BUY")

        instrument = (
            session.query(Instrument)
            .filter(
                Instrument.tradingsymbol == order.tradingsymbol,
                Instrument.exchange == order.exchange,
            )
            .first()
        )
        if instrument is None:
            log.error("_on_entry_filled: Instrument %s/%s not found", order.exchange, order.tradingsymbol)
            session.commit()
            return

        fill_price = event.fill_price
        rr = state.get_rr_ratio(settings.RR_RATIO)
        if instrument_type == "EQ":
            sl_distance = fill_price * settings.EQUITY_SL_PCT
            target_distance = rr * sl_distance
            sl_price = fill_price - sl_distance
            target_price = fill_price + target_distance
        elif instrument_type == "FUT":
            # sl_distance is always price-per-unit (FUTURES_SL_PCT × fill_price).
            # Do NOT pull from meta — the NG branch stored the lot-scaled INR value
            # which, subtracted from fill_price, produces a negative trigger price.
            sl_distance = fill_price * settings.FUTURES_SL_PCT
            target_distance = rr * sl_distance
            if entry_side == "BUY":
                sl_price = fill_price - sl_distance
                target_price = fill_price + target_distance
            else:  # short futures
                sl_price = fill_price + sl_distance
                target_price = fill_price - target_distance
        else:  # CE / PE options
            # Straddle legs use a wider per-leg SL (default 3× entry premium)
            # to avoid premature exits from normal vol spikes.  Non-straddle
            # legs use the standard SL_PREMIUM_PCT.
            if meta.get("straddle_id") and entry_side == "SELL":
                sl_pct = settings.STRADDLE_PER_LEG_SL_MULTIPLIER - 1.0
            else:
                sl_pct = state.get_sl_pct(settings.SL_PREMIUM_PCT)
            sl_risk_pct = fill_price * sl_pct          # percentage-based SL distance
            qty = event.fill_qty

            # Ensure the SL band is wide enough that max INR loss >= ₹1000.
            # MCX options have lot_size=1 in instruments CSV but each lot represents
            # MCX_LOT_UNITS underlying units (e.g. 10 barrels for CRUDEOILM).
            # Use effective monetary qty so the floor is per-unit price distance, not per-lot.
            _mcx_units = (
                settings.MCX_LOT_UNITS.get(meta.get("underlying", order.tradingsymbol), 1)
                if order.exchange == "MCX" else 1
            )
            _eff_qty = qty * _mcx_units
            _MIN_GTT_LOSS_INR = 1000.0
            min_loss_dist = _MIN_GTT_LOSS_INR / _eff_qty if _eff_qty > 0 else sl_risk_pct
            sl_distance = max(sl_risk_pct, min_loss_dist, 1.0)
            # Target keeps original %-based distance (preserves R:R intent) with ₹1 floor.
            target_distance = max(rr * sl_risk_pct, 1.0)

            if entry_side == "BUY":
                # Long: SL when premium falls, target when it rises
                sl_price = max(fill_price - sl_distance, 0.05)
                target_price = fill_price + target_distance
            else:
                # Short (written option): SL when premium rises, target when it falls
                sl_price = fill_price + sl_distance
                # Formula target: % drops as premium rises (quicker exit on expensive options).
                # /control override takes priority when explicitly set.
                _manual_pct = state.get_sell_options_profit_pct(None)
                if _manual_pct is not None:
                    _profit_pct = _manual_pct
                else:
                    _profit_pct = max(
                        settings.SELL_OPTIONS_PROFIT_FLOOR,
                        settings.SELL_OPTIONS_PROFIT_BASE - fill_price * settings.SELL_OPTIONS_PROFIT_SLOPE,
                    )
                target_price = max(fill_price * (1.0 - _profit_pct), 0.05)
                log.info(
                    "_on_entry_filled: %s fill=%.2f profit_target=%.0f%% → target=%.2f",
                    order.tradingsymbol, fill_price, _profit_pct * 100, target_price,
                )

        # Round SL and target to the instrument's minimum tick size so GTT
        # trigger prices are always valid tradeable prices (MCX tick: 0.05–0.10).
        _tick = (instrument.tick_size or 0.01) if instrument else 0.01
        sl_price = _round_to_tick(sl_price, _tick)
        target_price = _round_to_tick(target_price, _tick)
        # Re-apply floors after rounding (options premiums must stay positive).
        if instrument_type not in ("EQ", "FUT"):
            sl_price = max(sl_price, _tick)
            target_price = max(target_price, _tick)

        position = Position(
            order_id=order.id,
            exchange=order.exchange,
            tradingsymbol=order.tradingsymbol,
            underlying=meta.get("underlying", order.tradingsymbol),
            instrument_type=instrument_type,
            entry_premium=fill_price,
            current_sl=sl_price,
            quantity=event.fill_qty,
            lot_size=instrument.lot_size,
            opened_at=now,
            last_updated_at=now,
        )
        session.add(position)
        session.flush()  # get position.id

        # Pre-compute buffered GTT limits so place call AND Gtt row store the same values.
        from app.orders import compute_oco_limits
        _sl_limit_d, _tgt_limit_d = compute_oco_limits(
            sl_price, target_price, entry_side, settings.OCO_SLIPPAGE_BUFFER_PCT,
        )

        kite_gtt_id = None
        gtt_error: str | None = None
        if not _dry_run(settings):
            from decimal import Decimal
            kite_client = get_session_manager().get_kite()

            # Pre-check: fetch current LTP to detect if the GTT band is already breached.
            # For a short option this happens when the premium collapses to/below the
            # target trigger before we can place the GTT (cheap options, ~5-min delay).
            _ltp_now: float | None = None
            _needs_immediate_exit = False
            try:
                _ltp_data = kite_client.ltp(f"{order.exchange}:{order.tradingsymbol}")
                _ltp_now = float(_ltp_data[f"{order.exchange}:{order.tradingsymbol}"]["last_price"])
                if entry_side == "SELL":
                    _needs_immediate_exit = _ltp_now >= sl_price or _ltp_now <= target_price
                else:
                    _needs_immediate_exit = _ltp_now <= sl_price or _ltp_now >= target_price
                if _needs_immediate_exit:
                    log.warning(
                        "_on_entry_filled: LTP %.4f already outside GTT band "
                        "[%.4f–%.4f] for %s — will square off immediately",
                        _ltp_now, target_price, sl_price, order.tradingsymbol,
                    )
            except Exception as _ltp_exc:
                log.warning(
                    "_on_entry_filled: LTP pre-fetch failed for %s: %s",
                    order.tradingsymbol, _ltp_exc,
                )

            if not _needs_immediate_exit:
                try:
                    kite_gtt_id = place_gtt_oco(
                        kite_client,
                        instrument,
                        event.fill_qty,
                        sl_trigger=Decimal(str(sl_price)),
                        sl_limit=_sl_limit_d,
                        target_trigger=Decimal(str(target_price)),
                        target_limit=_tgt_limit_d,
                        last_price=Decimal(str(fill_price)),
                        product=order.product,
                        entry_side=entry_side,
                    )
                except Exception as exc:
                    gtt_error = str(exc)
                    _needs_immediate_exit = "trigger already met" in str(exc).lower()
                    log.error(
                        "_on_entry_filled: GTT placement failed for %s — %s. %s",
                        order.tradingsymbol, exc,
                        "Attempting immediate square-off." if _needs_immediate_exit
                        else "Position saved without SL; manual intervention required.",
                        exc_info=True,
                    )
                    session.add(AppError(
                        alert_id=order.alert_id,
                        error_type="GttPlacementError",
                        message=f"GTT OCO failed for {order.tradingsymbol}: {exc}",
                        traceback=traceback.format_exc(),
                        occurred_at=now,
                    ))

            # Square off immediately if the GTT band was already breached (either from
            # the LTP pre-check or a "Trigger already met" rejection from Kite).
            if _needs_immediate_exit:
                try:
                    _sq_order_id = square_off(
                        kite_client, instrument, event.fill_qty,
                        product=order.product, entry_side=entry_side,
                    )
                    _sq_msg = (
                        f"GTT band [{target_price:.4f}–{sl_price:.4f}] already breached "
                        f"(LTP {_ltp_now}); immediate square-off order {_sq_order_id}"
                    )
                    log.info("_on_entry_filled: %s for %s", _sq_msg, order.tradingsymbol)
                    gtt_error = ((gtt_error + " | ") if gtt_error else "") + _sq_msg
                except Exception as _sq_exc:
                    _sq_msg = (
                        f"GTT band breached AND immediate square-off FAILED "
                        f"for {order.tradingsymbol}: {_sq_exc}"
                    )
                    log.error("_on_entry_filled: CRITICAL — %s", _sq_msg, exc_info=True)
                    gtt_error = ((gtt_error + " | ") if gtt_error else "") + _sq_msg
                    session.add(AppError(
                        alert_id=order.alert_id,
                        error_type="GttPlacementError",
                        message=_sq_msg,
                        traceback=traceback.format_exc(),
                        occurred_at=now,
                    ))
        else:
            log.info(
                "_on_entry_filled DRY_RUN [%s]: would place GTT OCO sl=%.4f target=%.4f for %s",
                entry_side, sl_price, target_price, order.tradingsymbol,
            )

        gtt = Gtt(
            order_id=order.id,
            kite_gtt_id=kite_gtt_id,
            gtt_type="OCO",
            exchange=order.exchange,
            tradingsymbol=order.tradingsymbol,
            sl_trigger=sl_price,
            target_trigger=target_price,
            sl_order_price=float(_sl_limit_d),
            target_order_price=float(_tgt_limit_d),
            last_price_at_placement=fill_price,
            status="ACTIVE" if (not _dry_run(settings) and gtt_error is None) else ("GTT_FAILED" if gtt_error else "DRY_RUN"),
            placed_at=now,
            updated_at=now,
            dry_run=_dry_run(settings),
        )
        session.add(gtt)
        session.flush()  # get gtt.id

        position.gtt_id = gtt.id
        session.commit()

        # Start trailing the SL via live ticks (only for real GTTs, not dry-run, and when enabled)
        if _trailing_manager is not None and kite_gtt_id is not None and state.is_trailing_enabled():
            if instrument_type == "EQ":
                trail_sl_pct = settings.EQUITY_SL_PCT
            elif instrument_type == "FUT":
                trail_sl_pct = settings.FUTURES_SL_PCT
            elif meta.get("straddle_id") and entry_side == "SELL":
                trail_sl_pct = settings.STRADDLE_PER_LEG_SL_MULTIPLIER - 1.0
            else:
                trail_sl_pct = state.get_sl_pct(settings.SL_PREMIUM_PCT)
            _trailing_manager.register(
                tradingsymbol=order.tradingsymbol,
                instrument_token=instrument.instrument_token,
                exchange=order.exchange,
                entry_side=entry_side,
                sl_pct=trail_sl_pct,
                qty=event.fill_qty,
                product=order.product,
                target_price=target_price,
                initial_sl=sl_price,
                fill_price=fill_price,
                gtt_db_id=gtt.id,
                kite_gtt_id=kite_gtt_id,
                tick_size=(instrument.tick_size or 0.01) if instrument else 0.01,
            )
            if _watcher is not None:
                _watcher.subscribe([instrument.instrument_token])


# ── Restore trailing SL after restart ────────────────────────────────────────

def _restore_trailing_sl() -> None:
    """Re-register TrailingSL for all open positions after a container restart.

    Trailing state is in-memory only; every restart wipes it.  This function
    reads active GTTs from the DB and re-registers them so the trailing SL
    resumes from where it left off without widening any already-locked SL.
    """
    if _trailing_manager is None or _watcher is None or _SessionFactory is None:
        return
    settings = get_settings()
    try:
        with _SessionFactory() as session:
            from app.storage import Gtt, Instrument, Order, Position
            rows = (
                session.query(Position, Gtt, Order)
                .join(Gtt, Position.gtt_id == Gtt.id)
                .join(Order, Position.order_id == Order.id)
                .filter(Gtt.status == "ACTIVE", Gtt.dry_run == False)  # noqa: E712
                .all()
            )
            restored = 0
            for pos, gtt, order in rows:
                instr = (
                    session.query(Instrument)
                    .filter(
                        Instrument.tradingsymbol == pos.tradingsymbol,
                        Instrument.exchange == pos.exchange,
                    )
                    .first()
                )
                if instr is None:
                    log.warning(
                        "_restore_trailing_sl: instrument not found %s/%s — skipping",
                        pos.exchange, pos.tradingsymbol,
                    )
                    continue

                # Match sl_pct logic from _on_entry_filled
                if pos.instrument_type == "EQ":
                    sl_pct = settings.EQUITY_SL_PCT
                elif pos.instrument_type == "FUT":
                    sl_pct = settings.FUTURES_SL_PCT
                elif order.straddle_id and order.transaction_type == "SELL":
                    sl_pct = settings.STRADDLE_PER_LEG_SL_MULTIPLIER - 1.0
                else:
                    sl_pct = state.get_sl_pct(settings.SL_PREMIUM_PCT)

                current_sl = float(gtt.sl_trigger)
                entry_side = order.transaction_type

                # Derive the implied best_price from the current (trailed) SL so
                # the manager resumes from the correct anchor rather than the original fill.
                # For SELL: current_sl = best_price × (1 + sl_pct)  → best_price = current_sl / (1 + sl_pct)
                # For BUY:  current_sl = best_price × (1 − sl_pct)  → best_price = current_sl / (1 − sl_pct)
                if entry_side == "SELL":
                    implied_best = current_sl / (1.0 + sl_pct)
                else:
                    denom = 1.0 - sl_pct
                    implied_best = current_sl / denom if denom > 0 else current_sl

                _trailing_manager.register(
                    tradingsymbol=pos.tradingsymbol,
                    instrument_token=instr.instrument_token,
                    exchange=pos.exchange,
                    entry_side=entry_side,
                    sl_pct=sl_pct,
                    qty=order.quantity,
                    product=order.product,
                    target_price=float(gtt.target_trigger),
                    initial_sl=current_sl,
                    fill_price=implied_best,
                    gtt_db_id=gtt.id,
                    kite_gtt_id=gtt.kite_gtt_id,
                    tick_size=float(instr.tick_size or 0.01),
                )
                _watcher.subscribe([instr.instrument_token])
                restored += 1

        log.info("_restore_trailing_sl: re-registered %d open position(s)", restored)
    except Exception as exc:
        log.error("_restore_trailing_sl: failed — %s", exc, exc_info=True)


# ── GttFilledEvent callback ───────────────────────────────────────────────────

def _on_gtt_filled(event: GttFilledEvent) -> None:
    """Consume a GTT exit fill: compute PnL and persist via risk.record_trade_result."""
    if _SessionFactory is None:
        log.error("_on_gtt_filled fired but _SessionFactory is None")
        return
    with _SessionFactory() as session:
        position = (
            session.query(Position)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol == event.tradingsymbol,
                ClosedTrade.id == None,  # noqa: E711
            )
            .first()
        )
        if position is None:
            log.warning("_on_gtt_filled: no open position for %s", event.tradingsymbol)
            return

        entry_order = session.query(Order).filter(Order.id == position.order_id).first()
        if entry_order is None:
            log.error("_on_gtt_filled: no Order for position %d", position.id)
            return

        entry_price = Decimal(str(position.entry_premium))
        fill_price = Decimal(str(event.fill_price))
        qty = Decimal(str(event.fill_qty))
        # MCX: LTP and fill prices are per underlying unit; qty is in lots.
        # Multiply by units-per-lot to get true INR PnL.
        mcx_units = Decimal(str(
            get_settings().MCX_LOT_UNITS.get(position.underlying, 1)
            if position.exchange == "MCX" else 1
        ))

        if event.transaction_type == "SELL":
            pnl = (fill_price - entry_price) * qty * mcx_units
        else:
            pnl = -(fill_price - entry_price) * qty * mcx_units

        # Look up instrument token before committing so we can unregister trailing
        _instr_token: int | None = None
        if _trailing_manager is not None:
            instr = (
                session.query(Instrument)
                .filter(
                    Instrument.tradingsymbol == event.tradingsymbol,
                    Instrument.exchange == position.exchange,
                )
                .first()
            )
            _instr_token = instr.instrument_token if instr else None

        now = datetime.now(IST)
        risk.record_trade_result(session, entry_order.kite_order_id, pnl, now)
        session.commit()
        log.info(
            "_on_gtt_filled: %s pnl=%.2f order=%s",
            event.tradingsymbol, float(pnl), entry_order.kite_order_id,
        )

    # Stop trailing now that the position is closed
    if _trailing_manager is not None and _instr_token is not None:
        _trailing_manager.unregister(_instr_token)
        if _watcher is not None:
            _watcher.unsubscribe([_instr_token])


# ── Short straddle executor ───────────────────────────────────────────────────

def _process_straddle(alert_id: int, entry_data: dict, settings: Settings) -> None:
    """Place a short straddle: SELL CE + SELL PE simultaneously on MCX Natural Gas.

    Called as a BackgroundTask from execute_voice_straddle().  Uses
    ThreadPoolExecutor so both legs hit the exchange at the same instant.
    _on_entry_filled handles GTT OCO placement per leg once fills arrive.
    """
    import uuid as _uuid
    from concurrent.futures import ThreadPoolExecutor

    if _SessionFactory is None:
        log.error("_process_straddle: _SessionFactory is None")
        return

    with _SessionFactory() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            log.error("_process_straddle: Alert %d not found", alert_id)
            return

        now = datetime.now(IST)
        dry = _dry_run(settings)
        product = settings.PRODUCT_TYPE.value

        # Risk gates (straddle needs 2 new positions)
        ok, reason = _check_risk_guards(session, settings)
        if not ok:
            log.warning("_process_straddle %d blocked: %s", alert_id, reason)
            alert.processed = True
            session.commit()
            return

        current_pos = _open_position_count(session)
        eff_max_pos = state.get_max_open_positions(settings.MAX_OPEN_POSITIONS)
        if current_pos + 2 > eff_max_pos:
            log.warning(
                "_process_straddle %d blocked: would open 2 positions "
                "(current=%d max=%d)", alert_id, current_pos, eff_max_pos,
            )
            alert.processed = True
            session.commit()
            return

        # Resolve instrument rows from symbols stored in pending_entry
        ce_sym = entry_data.get("_straddle_ce_symbol", "")
        pe_sym = entry_data.get("_straddle_pe_symbol", "")
        if not ce_sym or not pe_sym:
            log.error("_process_straddle %d: missing CE/PE symbols in entry_data", alert_id)
            alert.processed = True
            session.commit()
            return

        _straddle_exchange = entry_data.get("exchange", "MCX")
        ce_instr = (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == ce_sym, Instrument.exchange == _straddle_exchange)
            .first()
        )
        pe_instr = (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == pe_sym, Instrument.exchange == _straddle_exchange)
            .first()
        )
        if ce_instr is None or pe_instr is None:
            log.error(
                "_process_straddle %d: instrument(s) not found CE=%s PE=%s",
                alert_id, ce_sym, pe_sym,
            )
            alert.processed = True
            session.commit()
            return

        quantity  = int(entry_data.get("quantity", 1))
        total_qty = ce_instr.lot_size * quantity
        underlying = entry_data.get("underlying", ce_instr.name)
        force_spread = bool(entry_data.get("_straddle_force_spread", False))

        # Execution-time re-validation (spread only; margin already checked at transcribe)
        if not dry and not force_spread:
            try:
                from app.voice.straddle import validate_straddle
                _kite_check = get_session_manager().get_kite()
                _sv = validate_straddle(
                    underlying, quantity, _kite_check, session, settings, now,
                    exchange=entry_data.get("exchange", "MCX"),
                )
                if not _sv.margin_ok:
                    log.error(
                        "_process_straddle %d: margin now insufficient at execution (%s) — aborting",
                        alert_id, _sv.block_reason,
                    )
                    alert.processed = True
                    session.commit()
                    return
                if not _sv.spread_ok:
                    log.warning(
                        "_process_straddle %d: spread deteriorated at execution (%s) — proceeding (force)",
                        alert_id, _sv.block_reason,
                    )
            except Exception as exc:
                log.warning("_process_straddle %d: re-validation skipped (%s)", alert_id, exc)

        # Place both legs concurrently
        ce_kite_id = pe_kite_id = None
        ce_err = pe_err = None

        if not dry:
            kite_client = get_session_manager().get_kite()
            timeout = settings.STRADDLE_FILL_TIMEOUT_SECS

            def _place(instr):
                return place_entry(kite_client, instr, "SELL", total_qty, "MARKET", product)

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_ce = pool.submit(_place, ce_instr)
                fut_pe = pool.submit(_place, pe_instr)
                try:
                    ce_kite_id = fut_ce.result(timeout=timeout)
                except Exception as exc:
                    ce_err = str(exc)
                    log.error("_process_straddle %d: CE placement failed: %s", alert_id, exc)
                try:
                    pe_kite_id = fut_pe.result(timeout=timeout)
                except Exception as exc:
                    pe_err = str(exc)
                    log.error("_process_straddle %d: PE placement failed: %s", alert_id, exc)

            # Partial fill compensation: cancel the filled leg if the other failed
            if ce_kite_id and not pe_kite_id:
                log.error(
                    "_process_straddle %d: CE filled but PE failed (%s) — cancelling CE %s",
                    alert_id, pe_err, ce_kite_id,
                )
                try:
                    kite_client.cancel_order(variety="regular", order_id=ce_kite_id)
                except Exception as exc:
                    log.error(
                        "_process_straddle %d: CRITICAL — cannot cancel CE %s: %s "
                        "— MANUAL INTERVENTION REQUIRED", alert_id, ce_kite_id, exc,
                    )
                alert.processed = True
                session.commit()
                return

            if pe_kite_id and not ce_kite_id:
                log.error(
                    "_process_straddle %d: PE filled but CE failed (%s) — cancelling PE %s",
                    alert_id, ce_err, pe_kite_id,
                )
                try:
                    kite_client.cancel_order(variety="regular", order_id=pe_kite_id)
                except Exception as exc:
                    log.error(
                        "_process_straddle %d: CRITICAL — cannot cancel PE %s: %s "
                        "— MANUAL INTERVENTION REQUIRED", alert_id, pe_kite_id, exc,
                    )
                alert.processed = True
                session.commit()
                return
        else:
            log.info(
                "_process_straddle %d DRY_RUN — would SELL %s + SELL %s qty=%d product=%s",
                alert_id, ce_sym, pe_sym, total_qty, product,
            )

        # Shared straddle_id links the two Order rows for combined P&L queries
        straddle_id = str(_uuid.uuid4())

        for kite_oid, instr_type in [(ce_kite_id, "CE"), (pe_kite_id, "PE")]:
            if kite_oid:
                _pending_order_meta[kite_oid] = {
                    "instrument_type": instr_type,
                    "entry_side": "SELL",
                    "underlying": underlying,
                    "straddle_id": straddle_id,
                    "dry_run": False,
                }

        now2 = datetime.now(IST)
        status = "PENDING" if not dry else "DRY_RUN"
        for kite_oid, sym, instr in [
            (ce_kite_id, ce_sym, ce_instr),
            (pe_kite_id, pe_sym, pe_instr),
        ]:
            o = Order(
                alert_id=alert_id,
                kite_order_id=kite_oid,
                variety="regular",
                exchange=_straddle_exchange,
                tradingsymbol=sym,
                transaction_type="SELL",
                order_type="MARKET",
                product=product,
                quantity=total_qty,
                status=status,
                placed_at=now2,
                updated_at=now2,
                dry_run=dry,
                straddle_id=straddle_id,
            )
            session.add(o)

        alert.processed = True
        session.commit()

        if not dry and _watcher is not None:
            for kite_oid in [ce_kite_id, pe_kite_id]:
                if kite_oid:
                    _watcher.watch_order(kite_oid, kite_fetcher=get_session_manager().get_kite)
        elif not dry and _watcher is None:
            log.error("_process_straddle %d: _watcher is None — GTTs will NOT be placed", alert_id)

        log.info(
            "_process_straddle %d complete: straddle_id=%s CE=%s PE=%s dry=%s",
            alert_id, straddle_id[:8], ce_kite_id, pe_kite_id, dry,
        )


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SessionFactory, _watcher, _trailing_manager, _scheduler
    if _SessionFactory is None:
        from sqlalchemy.orm import sessionmaker
        engine = init_db(get_settings().DATABASE_URL)
        _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
        _register_factory(_SessionFactory)
    from app.trailing import TrailingSlManager
    _trailing_manager = TrailingSlManager(
        session_factory=_SessionFactory,
        kite_fetcher=get_session_manager().get_kite,
    )
    _watcher = OrderWatcher(
        on_entry_filled=_on_entry_filled,
        on_gtt_filled=_on_gtt_filled,
        on_tick_callback=_trailing_manager.on_ticks,
    )
    # Order matters: load_overrides_from_disk() FIRST so module-default
    # globals are replaced by persisted values. set_trade_mode() triggers a
    # save, so calling it before load would write the empty in-memory state
    # to disk and wipe everything the user persisted.
    state.load_overrides_from_disk()
    if not state.get_trade_mode():
        state.set_trade_mode(get_settings().TRADE_MODE.value)
    def _watcher_restart(api_key: str, access_token: str) -> None:
        if _watcher is not None:
            _watcher.restart(api_key=api_key, access_token=access_token)

    _scheduler = make_scheduler(
        session_factory=_SessionFactory,
        watcher_restart_fn=_watcher_restart,
    )
    _scheduler.start()
    daily_session_check(now=datetime.now(IST))
    # Start watcher immediately if a valid token is already stored, so fill
    # events are not missed after a container restart (the watcher is also
    # started in /kite/callback for fresh OAuth logins).
    if not state.SESSION_INVALID:
        try:
            result = get_session_manager()._load_token()
            if result is not None:
                _stored_token, _ = result
                _watcher.start(
                    api_key=get_settings().KITE_API_KEY,
                    access_token=_stored_token,
                )
                log.info("OrderWatcher started at startup with stored token")
                _restore_trailing_sl()
        except Exception as _exc:
            log.warning("Could not start OrderWatcher at startup: %s", _exc)
    # Run instrument refresh in a background thread so the server starts
    # accepting webhooks immediately instead of blocking for 30+ seconds.
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, refresh_instruments_job, _SessionFactory)
    yield
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="tv-zerodha-bot", version="0.1.0", lifespan=lifespan)
_security = HTTPBasic(auto_error=False)

from app.webauthn_routes import router as _webauthn_router  # noqa: E402
app.include_router(_webauthn_router)

from app.routes.voice import router as _voice_router  # noqa: E402
from app.routes.admin_voice import router as _admin_voice_router  # noqa: E402
app.include_router(_voice_router)
app.include_router(_admin_voice_router)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    body = await request.body()
    log.error(
        "422 validation error on %s | content-type=%r | body=%r | errors=%s",
        request.url.path,
        request.headers.get("content-type", "<missing>"),
        body[:500],
        exc.errors(),
    )
    safe_errors = [
        {**e, "input": e["input"].decode("utf-8", errors="replace") if isinstance(e.get("input"), bytes) else e.get("input")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})


# ── Dependencies ──────────────────────────────────────────────────────────────

def get_current_settings() -> Settings:
    return get_settings()


def get_db_session() -> Any:
    if _SessionFactory is None:
        raise RuntimeError("Database not initialized — lifespan must run first")
    with _SessionFactory() as session:
        yield session


def _auth_guard(
    request: Request,
    response: Response,
    credentials: HTTPBasicCredentials | None = Depends(_security),
    settings: Settings = Depends(get_current_settings),
    db: Session = Depends(get_db_session),
) -> None:
    # 1. Valid session cookie (WebAuthn or password login)
    token = request.cookies.get("zb_session")
    if token:
        ws = (
            db.query(WebSession)
            .filter(
                WebSession.token == token,
                WebSession.expires_at > datetime.now(timezone.utc),
            )
            .first()
        )
        if ws:
            return

    # 2. Dev mode — no password configured
    if not settings.DASHBOARD_PASSWORD:
        _stamp_session(db, response)
        return

    # 3. HTTP Basic Auth — issue a session cookie so Face ID setup works immediately
    if credentials is not None:
        ok = secrets.compare_digest(
            credentials.username, settings.DASHBOARD_USERNAME
        ) and secrets.compare_digest(credentials.password, settings.DASHBOARD_PASSWORD)
        if ok:
            _stamp_session(db, response)
            return

    # 4. Redirect to login page
    raise HTTPException(
        status_code=303,
        headers={"Location": "/login"},
        detail="Authentication required",
    )


def _stamp_session(db: Session, response: Response) -> None:
    """Issue a 12-hour session cookie if the request doesn't already have one."""
    token = secrets.token_hex(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=12)
    db.add(WebSession(token=token, created_at=datetime.now(timezone.utc), expires_at=expires))
    db.commit()
    response.set_cookie(
        key="zb_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=12 * 3600,
        path="/",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _build_alert_row(payload: AlertPayload, raw: str, ikey: str) -> Alert:
    return Alert(
        received_at=payload.timestamp,
        strategy_id=payload.alert_id,
        tv_ticker=payload.symbol,
        tv_exchange="",
        action=payload.action,
        order_type=None,
        entry_price=float(payload.price),
        stop_loss=None,
        sl_percent=None,
        atr=None,
        quantity_hint=None,
        product="NRML",
        tv_time=payload.timestamp.isoformat(),
        bar_time=None,
        interval=payload.timeframe,
        idempotency_key=ikey,
        raw_payload=raw,
        processed=False,
    )


# ── Background task ───────────────────────────────────────────────────────────

def _fail_alert(session: Any, alert: Alert, error_type: str, exc: BaseException) -> None:
    """Persist an AppError row, leave alert.processed=False, and log with traceback.

    Caller must commit() after this returns — keeps commit ownership at the call site.
    alert.processed is intentionally NOT set to True so the /dashboard shows the alert
    as unhandled and operators know it needs attention.
    """
    log.error("Alert %d [%s]: %s", alert.id, error_type, exc, exc_info=True)
    session.add(AppError(
        alert_id=alert.id,
        error_type=error_type,
        message=str(exc),
        traceback=traceback.format_exc(),
        occurred_at=datetime.now(IST),
    ))




def _process_alert(alert_id: int, alert_data: AlertPayload, settings: Settings) -> None:
    with _SessionFactory() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            log.error("Alert %d not found in DB (background task)", alert_id)
            return

        underlying = resolve_underlying(alert_data.symbol, settings=settings)
        now = alert_data.timestamp
        product = settings.PRODUCT_TYPE.value

        # ── Stock mode routing (toggleable from /control) ─────────────────────
        # For non-indexed NSE symbols (stocks), the /control STOCK_MODE toggle
        # decides whether BUY/SELL trades as EQUITY (CNC) or F&O OPTIONS.
        # Index symbols (segment NFO/BFO) and MCX commodities (segment MCX) are
        # untouched — they always route as today. Default "FNO" preserves the
        # pre-existing webhook behavior so this is a no-op until the toggle flips.
        if (
            alert_data.action in ("BUY", "SELL")
            and underlying.segment == "NSE"
        ):
            _stock_mode = state.get_stock_mode()
            if _stock_mode == "EQUITY" and alert_data.instrument_type != "EQUITY":
                alert_data = alert_data.model_copy(update={"instrument_type": "EQUITY"})
                log.info(
                    "Alert %d: STOCK_MODE=EQUITY → routing %s as equity CNC",
                    alert_id, underlying.name,
                )
            elif _stock_mode == "FNO" and alert_data.instrument_type != "OPTIONS":
                alert_data = alert_data.model_copy(update={"instrument_type": "OPTIONS"})
                log.info(
                    "Alert %d: STOCK_MODE=FNO → routing %s as options",
                    alert_id, underlying.name,
                )

        if alert_data.action == "TRAIL":
            log.info(
                "Alert %d: TRAIL action ignored — tick-based trailing SL is active automatically",
                alert_id,
            )
            alert.processed = True
            session.commit()
            return

        if state.is_emergency_stop():
            log.warning("Alert %d: EMERGENCY STOP active — all trading halted", alert_id)
            alert.processed = True
            session.commit()
            return

        if not _dry_run(settings):
            token_info = get_session_manager().get_token_info()
            if not token_info["is_valid"] or state.SESSION_INVALID:
                reason = token_info.get("reason") or "session marked invalid by scheduler"
                log.warning(
                    "Alert %d: order blocked — %s. Re-login at /kite/login",
                    alert_id, reason,
                )
                state.set_session_invalid(True)
                alert.processed = True
                session.commit()
                return

        if not _dry_run(settings):
            try:
                risk.check_risk_gates(session, now)
            except risk.RiskHaltError as exc:
                log.warning("Alert %d: risk halt — %s", alert_id, exc.reason)
                alert.processed = True
                session.commit()
                return

        # ── Daily profit target circuit breaker ───────────────────────────────
        if not _dry_run(settings):
            _profit_target = state.get_daily_profit_target(settings.DAILY_PROFIT_TARGET)
            if _profit_target > 0:
                _today_pnl = risk.daily_realized_pnl(session, now)
                if _today_pnl >= Decimal(str(_profit_target)):
                    log.info(
                        "Alert %d: blocked — daily profit target ₹%.0f reached (realized ₹%.0f)",
                        alert_id, _profit_target, float(_today_pnl),
                    )
                    alert.processed = True
                    session.commit()
                    return

        # ── Time-of-day entry filter ──────────────────────────────────────────
        # Voice orders are manual commands — skip the time gate.
        _is_voice = bool(alert.strategy_id and alert.strategy_id.startswith(("voice_", "straddle_")))
        if alert_data.action in ("BUY", "SELL") and not _is_voice:
            _entry_start = _parse_hhmm(state.get_entry_window_start(settings.ENTRY_WINDOW_START))
            # MCX commodities trade until 23:30 IST — use a separate end cutoff.
            _raw = alert_data.symbol.upper().split(":")[-1]
            if _raw.endswith("1!"):
                _raw = _raw[:-2]
            _is_mcx = _raw in _MCX_NAMES or _raw in settings.NATURAL_GAS_NAMES
            _mcx_end = "23:00"
            _entry_end = _parse_hhmm(_mcx_end if _is_mcx else state.get_entry_window_end(settings.ENTRY_WINDOW_END))
            _window_end_label = _mcx_end if _is_mcx else settings.ENTRY_WINDOW_END
            _now_time = now.time().replace(second=0, microsecond=0)
            if not (_entry_start <= _now_time <= _entry_end):
                log.info(
                    "Alert %d: blocked — outside entry window [%s–%s] at %s",
                    alert_id, settings.ENTRY_WINDOW_START, _window_end_label,
                    now.strftime("%H:%M"),
                )
                alert.processed = True
                session.commit()
                return

        # ── EQUITY (CNC) entry ────────────────────────────────────────────────
        # MCX symbols (CRUDEOILM, GOLD, etc.) sometimes arrive with instrument_type="EQUITY"
        # due to a TradingView alert template misconfiguration. Guard against misrouting them.
        if alert_data.instrument_type == "EQUITY" and underlying.segment != "MCX":
            if alert_data.action == "SELL":
                # SELL on equity = exit the long position; fall through to EXIT handler below
                log.info("Alert %d: EQUITY SELL → routing to EXIT for %s", alert_id, underlying.name)
                alert_data = alert_data.model_copy(update={"action": "EXIT", "symbol": underlying.name})
            else:
                if alert_data.action != "BUY":
                    log.warning(
                        "Alert %d: EQUITY unsupported action %s — skipping",
                        alert_id, alert_data.action,
                    )
                    alert.processed = True
                    session.commit()
                    return

                eq_instrument = (
                    session.query(Instrument)
                    .filter(
                        Instrument.tradingsymbol == underlying.name,
                        Instrument.exchange == "NSE",
                        Instrument.instrument_type == "EQ",
                    )
                    .first()
                )
                if eq_instrument is None:
                    log.error(
                        "Alert %d: EQUITY — %s not found in NSE instruments table",
                        alert_id, underlying.name,
                    )
                    alert.processed = True
                    session.commit()
                    return

                if not _dry_run(settings):
                    kite_client = get_session_manager().get_kite()
                    q_key = f"NSE:{underlying.name}"
                    q = kite_client.quote(q_key)
                    ltp = float(q[q_key]["last_price"])
                else:
                    ltp = float(alert_data.price)

                risk_amount = Decimal(str(state.get_capital_per_trade(settings.CAPITAL_PER_TRADE))) * settings.RISK_PCT
                sl_per_share = Decimal(str(ltp)) * Decimal(str(settings.EQUITY_SL_PCT))
                qty = floor(float(risk_amount / sl_per_share)) if sl_per_share > 0 else 0
                qty = min(qty, state.get_max_lots(settings.MAX_LOTS_PER_TRADE))
                if qty < 1:
                    log.warning(
                        "Alert %d: EQUITY sizing — 0 shares at ltp=%.2f risk_amount=%.0f",
                        alert_id, ltp, float(risk_amount),
                    )
                    alert.processed = True
                    session.commit()
                    return

                _eq_lp = alert_data.limit_price
                _eq_ot = "LIMIT" if _eq_lp else "MARKET"
                kite_order_id = None
                if not _dry_run(settings):
                    kite_order_id = place_entry(kite_client, eq_instrument, "BUY", qty, _eq_ot, "CNC", price=_eq_lp)
                    if kite_order_id:
                        _pending_order_meta[kite_order_id] = {
                            "instrument_type": "EQ",
                            "underlying": underlying.name,
                            "dry_run": False,
                        }
                else:
                    log.info(
                        "Alert %d: DRY_RUN — would BUY %d shares %s %s CNC at ltp=%.2f",
                        alert_id, qty, eq_instrument.tradingsymbol, _eq_ot, ltp,
                    )

                order = Order(
                    alert_id=alert_id,
                    kite_order_id=kite_order_id,
                    variety="regular",
                    exchange=eq_instrument.exchange,
                    tradingsymbol=eq_instrument.tradingsymbol,
                    transaction_type="BUY",
                    order_type=_eq_ot,
                    price=float(_eq_lp) if _eq_lp else None,
                    product="CNC",
                    quantity=qty,
                    status="PENDING" if not _dry_run(settings) else "DRY_RUN",
                    placed_at=now,
                    updated_at=now,
                    dry_run=_dry_run(settings),
                )
                session.add(order)
                session.flush()

                alert.processed = True
                session.commit()
                # Register AFTER commit so _on_entry_filled can find the Order row.
                if kite_order_id and not _dry_run(settings):
                    if _watcher is not None:
                        _watcher.watch_order(kite_order_id, kite_fetcher=get_session_manager().get_kite)
                    else:
                        log.error(
                            "Alert %d: _watcher is None — GTT will not be placed for %s",
                            alert_id, eq_instrument.tradingsymbol,
                        )
                return

        # ── EXIT ──────────────────────────────────────────────────────────────
        if alert_data.action == "EXIT":
            ts = underlying.name
            position = (
                session.query(Position)
                .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
                .filter(Position.tradingsymbol == ts, ClosedTrade.id == None)  # noqa: E711
                .first()
            )
            if position is None:
                log.warning("Alert %d: EXIT — no open position found for %s", alert_id, ts)
                alert.processed = True
                session.commit()
                return

            gtt = (
                session.query(Gtt)
                .filter(Gtt.order_id == position.order_id, Gtt.status != "CANCELLED")
                .first()
            )
            instrument = (
                session.query(Instrument)
                .filter(Instrument.tradingsymbol == ts, Instrument.exchange == position.exchange)
                .first()
            )

            # Use the product stored on the original entry order so that CNC
            # equity exits use CNC (not the global NRML options product type).
            entry_order = session.query(Order).filter(Order.id == position.order_id).first()
            exit_product = entry_order.product if entry_order else product

            if not _dry_run(settings):
                kite_client = get_session_manager().get_kite()
                if gtt and gtt.kite_gtt_id:
                    cancel_gtt(kite_client, gtt.kite_gtt_id)
                if gtt:
                    gtt.status = "CANCELLED"
                if instrument:
                    square_off(kite_client, instrument, position.quantity, exit_product)
            else:
                log.info("Alert %d: DRY_RUN — would EXIT %s qty=%d product=%s", alert_id, ts, position.quantity, exit_product)

            ct = ClosedTrade(
                position_id=position.id,
                exchange=position.exchange,
                tradingsymbol=position.tradingsymbol,
                entry_premium=position.entry_premium,
                exit_premium=0.0,
                pnl=0.0,
                exit_reason="MANUAL_EXIT",
                opened_at=position.opened_at,
                closed_at=now,
            )
            session.add(ct)
            alert.processed = True
            session.commit()
            return

        # ── Options entry (NFO + MCX; also NG when option_type is explicit) ──
        trade_mode = state.get_trade_mode()
        if alert_data.option_type in ("CE", "PE"):
            # Voice command with explicit option type: honor it directly
            flag = alert_data.option_type
            entry_side = "SELL" if alert_data.action == "SELL" else "BUY"
        elif trade_mode == "BUY_OPTIONS":
            flag = "CE" if alert_data.action == "BUY" else "PE"
            entry_side = "BUY"
        elif trade_mode == "SELL_OPTIONS":
            # Write the opposite type (PE on BUY signal, CE on SELL signal)
            flag = "PE" if alert_data.action == "BUY" else "CE"
            entry_side = "SELL"
        else:  # RANGE_SELL: sell same-direction option; only fires when ADX < threshold
            adx_threshold = state.get_adx_threshold(settings.ADX_THRESHOLD)
            if alert_data.adx is None or alert_data.adx >= adx_threshold:
                log.info(
                    "Alert %d: RANGE_SELL skipped -- ADX=%s (threshold=%.1f, need ADX < threshold)",
                    alert_id,
                    f"{alert_data.adx:.1f}" if alert_data.adx is not None else "not provided",
                    adx_threshold,
                )
                alert.processed = True
                session.commit()
                return
            flag = "CE" if alert_data.action == "BUY" else "PE"
            entry_side = "SELL"
        # Block new SELL_OPTIONS entries on weekly expiry day (voice = manual override, skip)
        _is_voice_order = bool(alert.strategy_id and alert.strategy_id.startswith(("voice_", "straddle_")))
        if entry_side == "SELL" and state.get_no_entry_on_expiry_day(settings.NO_ENTRY_ON_EXPIRY_DAY) and not _is_voice_order:
            _is_expiry = (
                session.query(Instrument)
                .filter(
                    Instrument.name == underlying.name,
                    Instrument.exchange.in_(["NSE", "NFO"]),
                    Instrument.expiry == now.date(),
                    Instrument.instrument_type.in_(["CE", "PE"]),
                )
                .first()
            ) is not None
            if _is_expiry:
                log.info(
                    "Alert %d: blocked — expiry day for %s, no new SELL_OPTIONS",
                    alert_id, underlying.name,
                )
                alert.processed = True
                session.commit()
                return
        log.info("[%s] Alert %d: %s signal → %s %s", trade_mode, alert_id, alert_data.action, entry_side, flag)
        if alert_data.option_type in ("CE", "PE"):
            # Voice explicit: derive delta from entry_side, not bot trade_mode
            target_delta = settings.SELL_OPTIONS_TARGET_DELTA if entry_side == "SELL" else settings.TARGET_DELTA
            delta_fallbacks = settings.SELL_OPTIONS_DELTA_FALLBACK_STEPS if entry_side == "SELL" else settings.DELTA_FALLBACK_STEPS
        else:
            _is_buying = trade_mode == "BUY_OPTIONS"
            target_delta = settings.TARGET_DELTA if _is_buying else settings.SELL_OPTIONS_TARGET_DELTA
            delta_fallbacks = settings.DELTA_FALLBACK_STEPS if _is_buying else settings.SELL_OPTIONS_DELTA_FALLBACK_STEPS

        if _dry_run(settings):
            log.info(
                "[%s] Alert %d: DRY_RUN — would %s %s option for %s (segment=%s)",
                trade_mode, alert_id, entry_side, flag, underlying.name, underlying.segment,
            )
            order = Order(
                alert_id=alert_id,
                kite_order_id=None,
                variety="regular",
                exchange=underlying.segment,
                tradingsymbol=underlying.name,
                transaction_type=entry_side,
                order_type="MARKET",
                product=product,
                quantity=0,
                status="DRY_RUN",
                placed_at=now,
                updated_at=now,
                dry_run=True,
            )
            session.add(order)
            alert.processed = True
            session.commit()
            return

        kite_client = get_session_manager().get_kite()

        # MCX commodities have no quotable spot index (unlike NSE:NIFTY 50).
        # Quote the near-month FUT tradingsymbol as a spot-price proxy instead.
        if underlying.segment == "MCX":
            mcx_spot_instr = (
                session.query(Instrument)
                .filter(
                    Instrument.name == underlying.name,
                    Instrument.instrument_type == "FUT",
                    Instrument.exchange == "MCX",
                    Instrument.expiry >= now.date(),
                )
                .order_by(Instrument.expiry)
                .first()
            )
            if mcx_spot_instr is None:
                log.error("Alert %d: no MCX FUT found for spot price of %s", alert_id, underlying.name)
                alert.processed = True
                session.commit()
                return
            spot_key = f"MCX:{mcx_spot_instr.tradingsymbol}"
            spot_quote = kite_client.quote(spot_key)
            spot_ltp = float(spot_quote[spot_key]["last_price"])
        else:
            spot_quote = kite_client.quote(underlying.spot_source)
            spot_ltp = float(spot_quote[underlying.spot_source]["last_price"])

        try:
            resolved = resolve_expiry(
                underlying.name, session, instrument_type=flag,
                segment=underlying.segment, now=now, settings=settings,
            )
        except NoEligibleExpiryError as exc:
            _fail_alert(session, alert, "NoEligibleExpiryError", exc)
            session.commit()
            return

        if alert_data.strike:
            # Voice command with specific strike: look up instrument directly
            from types import SimpleNamespace as _NS
            _vi = (
                session.query(Instrument)
                .filter(
                    Instrument.name == underlying.name,
                    Instrument.instrument_type == flag,
                    Instrument.exchange == underlying.segment,
                    Instrument.expiry == resolved.expiry_date,
                    Instrument.strike == float(alert_data.strike),
                )
                .first()
            )
            if _vi is None:
                _fail_alert(
                    session, alert, "NoValidStrikeError",
                    ValueError(
                        f"No {flag} at strike {alert_data.strike} for "
                        f"{underlying.name} expiry {resolved.expiry_date}"
                    ),
                )
                session.commit()
                return
            _vq_key = f"{underlying.segment}:{_vi.tradingsymbol}"
            option_ltp = float(kite_client.quote(_vq_key)[_vq_key]["last_price"])
            selection = _NS(instrument=_vi, option_ltp=option_ltp)
            lot_size = _vi.lot_size
        else:
            try:
                selection = select_strike(
                    underlying.name,
                    resolved.expiry_date,
                    flag,
                    kite_client,
                    spot_ltp,
                    session,
                    alert_id=alert_id,
                    segment=underlying.segment,
                    target_delta=target_delta,
                    settings=settings,
                )
            except NoValidStrikeError as exc:
                _fail_alert(session, alert, "NoValidStrikeError", exc)
                session.commit()
                return

            lot_size = selection.instrument.lot_size
            option_ltp = float(selection.option_ltp)
        # MCX: Kite quotes LTP per underlying unit (barrel/gram/kg) but orders are placed in lots.
        # Multiply by units-per-lot so sizing reflects the true premium per lot.
        mcx_units = settings.MCX_LOT_UNITS.get(underlying.name, 1) if underlying.segment == "MCX" else 1

        # Risk-based sizing: cap position so loss at SL ≤ capital_risk (mirrors NG futures).
        # For NSE (mcx_units=1): sl_per_unit = ltp×SL_PCT; compute_futures_qty divides by lot_size.
        # For MCX (lot_size=1):  sl_per_unit = ltp×mcx_units×SL_PCT (= risk per Kite lot directly).
        daily_remaining_opt = risk.daily_loss_remaining(session, now)
        capital_risk_opt = min(
            Decimal(str(state.get_capital_per_trade(settings.CAPITAL_PER_TRADE))) * settings.RISK_PCT,
            daily_remaining_opt,
        )
        sl_per_unit = Decimal(str(option_ltp * mcx_units)) * Decimal(str(state.get_sl_pct(settings.SL_PREMIUM_PCT)))
        qty = risk.compute_futures_qty(capital_risk_opt, sl_per_unit, Decimal(str(lot_size)))

        if qty < lot_size and alert_data.strike:
            # User explicitly named a strike — honour it at minimum 1 lot, don't override.
            log.warning(
                "Alert %d: explicit strike %s — risk budget ₹%.0f < 1 lot cost; "
                "placing minimum 1 lot as requested (strike override not allowed for voice)",
                alert_id, selection.instrument.tradingsymbol, float(capital_risk_opt),
            )
            qty = lot_size

        if qty < lot_size:
            log.warning(
                "Alert %d: options sizing — 0 lots at ltp=%.4f mcx_units=%d risk_budget=%.0f; "
                "retrying with lower deltas %s",
                alert_id, option_ltp, mcx_units,
                float(capital_risk_opt), delta_fallbacks,
            )
            fallback_selection = None
            for fallback_delta in delta_fallbacks:
                if fallback_delta >= target_delta:
                    continue
                try:
                    candidate = select_strike(
                        underlying.name,
                        resolved.expiry_date,
                        flag,
                        kite_client,
                        spot_ltp,
                        session,
                        alert_id=alert_id,
                        segment=underlying.segment,
                        target_delta=fallback_delta,
                        settings=settings,
                    )
                except NoValidStrikeError:
                    log.warning(
                        "Alert %d: no valid strike at fallback delta=%.2f",
                        alert_id, fallback_delta,
                    )
                    continue
                fallback_sl_per_unit = (
                    Decimal(str(float(candidate.option_ltp) * mcx_units))
                    * Decimal(str(state.get_sl_pct(settings.SL_PREMIUM_PCT)))
                )
                fallback_qty = risk.compute_futures_qty(
                    capital_risk_opt,
                    fallback_sl_per_unit,
                    Decimal(str(candidate.instrument.lot_size)),
                )
                if fallback_qty >= candidate.instrument.lot_size:
                    log.info(
                        "Alert %d: fallback delta=%.2f → %s ltp=%.4f fits risk_budget=%.0f",
                        alert_id, fallback_delta, candidate.instrument.tradingsymbol,
                        candidate.option_ltp, float(capital_risk_opt),
                    )
                    fallback_selection = candidate
                    break

            if fallback_selection is None:
                # Risk budget too small for any delta — force 1 lot at primary strike
                # rather than rejecting the trade entirely.
                log.info(
                    "Alert %d: risk budget ₹%.0f too small for any delta %s — forcing 1 lot at %s",
                    alert_id, float(capital_risk_opt), delta_fallbacks,
                    selection.instrument.tradingsymbol,
                )
                qty = lot_size  # selection already points to primary strike
            else:
                selection = fallback_selection
                lot_size = selection.instrument.lot_size
                option_ltp = float(selection.option_ltp)
                sl_per_unit = (
                    Decimal(str(option_ltp * mcx_units)) * Decimal(str(state.get_sl_pct(settings.SL_PREMIUM_PCT)))
                )
                qty = risk.compute_futures_qty(capital_risk_opt, sl_per_unit, Decimal(str(lot_size)))

        # Voice commands carry an explicit lot count (quantity_hint) — honor it
        # instead of the risk-based sizing above. TV alerts use risk sizing.
        if alert_data.option_type in ("CE", "PE") and alert.quantity_hint and alert.quantity_hint > 0:
            qty = min(alert.quantity_hint, state.get_max_lots(settings.MAX_LOTS_PER_TRADE)) * lot_size
        else:
            qty = min(qty, state.get_max_lots(settings.MAX_LOTS_PER_TRADE) * lot_size)

        lots_count = qty // lot_size
        total_premium = option_ltp * mcx_units * qty
        log.info(
            "[%s] Alert %d: placing %d lot(s) %s @ ltp=%.2f → total ₹%.0f (risk_budget=%.0f)",
            trade_mode, alert_id, lots_count, selection.instrument.tradingsymbol,
            option_ltp, total_premium, float(capital_risk_opt),
        )

        _opt_lp = alert_data.limit_price
        _opt_ot = "LIMIT" if _opt_lp else "MARKET"
        kite_order_id = place_entry(
            kite_client, selection.instrument, entry_side, qty, _opt_ot, product, price=_opt_lp
        )
        if kite_order_id:
            _pending_order_meta[kite_order_id] = {
                "instrument_type": flag,
                "underlying": underlying.name,
                "entry_side": entry_side,
                "dry_run": False,
            }

        order = Order(
            alert_id=alert_id,
            kite_order_id=kite_order_id,
            variety="regular",
            exchange=selection.instrument.exchange,
            tradingsymbol=selection.instrument.tradingsymbol,
            transaction_type=entry_side,
            order_type=_opt_ot,
            price=float(_opt_lp) if _opt_lp else None,
            product=product,
            quantity=qty,
            status="PENDING",
            placed_at=now,
            updated_at=now,
            dry_run=False,
        )
        session.add(order)
        session.flush()

        alert.processed = True
        session.commit()
        # Register AFTER commit so _on_entry_filled can find the Order row.
        # MARKET orders fill in milliseconds; committing first closes the race.
        if kite_order_id:
            if _watcher is not None:
                _watcher.watch_order(kite_order_id, kite_fetcher=get_session_manager().get_kite)
            else:
                log.error(
                    "Alert %d: _watcher is None — GTT will not be placed for %s",
                    alert_id, selection.instrument.tradingsymbol,
                )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_current_settings),
    session: Session = Depends(get_db_session),
) -> dict:
    client_ip = _get_client_ip(request)

    try:
        check_ip(client_ip, allowed_ips=settings.TV_ALLOWED_IPS)
    except IPBlockedError:
        raise HTTPException(status_code=401, detail="IP not in allowlist")

    try:
        check_rate_limit(client_ip)
    except RateLimitError:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # TradingView sends Content-Type: text/plain even for JSON bodies, so we
    # must parse manually instead of relying on FastAPI's automatic JSON binding.
    from starlette.requests import ClientDisconnect
    try:
        raw_body = await request.body()
    except ClientDisconnect:
        log.error("webhook: TradingView disconnected before body was delivered (ClientDisconnect) — alert lost")
        return Response(status_code=499)  # client closed request
    try:
        payload = AlertPayload.model_validate(json.loads(raw_body))
    except json.JSONDecodeError as exc:
        log.error(
            "webhook parse error | content-type=%r | body=%r | error=%s",
            request.headers.get("content-type", "<missing>"),
            raw_body[:500],
            exc,
        )
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}")
    except Exception as exc:
        log.error(
            "webhook validation error | content-type=%r | body=%r | error=%s",
            request.headers.get("content-type", "<missing>"),
            raw_body[:500],
            exc,
        )
        raise HTTPException(status_code=422, detail=str(exc))

    ikey = payload.alert_id
    if ikey in _idempotency_cache:
        return {"status": "duplicate"}
    _idempotency_cache[ikey] = True

    alert_row = _build_alert_row(payload, payload.model_dump_json(), ikey)
    session.add(alert_row)
    try:
        session.commit()
        session.refresh(alert_row)
    except IntegrityError:
        session.rollback()
        return {"status": "duplicate"}

    trades_today = _trades_today_count(session)
    eff_max_trades_wh = state.get_max_trades_per_day(settings.MAX_TRADES_PER_DAY)
    if trades_today >= eff_max_trades_wh:
        log.warning(
            "Alert %d: Max daily trades reached (%d/%d) — rejecting",
            alert_row.id, trades_today, eff_max_trades_wh,
        )
        return {"status": "rejected", "reason": "max_daily_trades"}

    background_tasks.add_task(_process_alert, alert_row.id, payload, settings)

    return {"status": "queued", "alert_id": alert_row.id}


@app.get("/kite/login")
async def kite_login() -> Response:
    """Redirect the browser to Kite's OAuth login page.

    After the user authenticates, Kite redirects to KITE_REDIRECT_URL/kite/callback
    with ?status=success&request_token=<token>.
    """
    from kiteconnect import KiteConnect
    settings = get_settings()
    kite = KiteConnect(api_key=settings.KITE_API_KEY)
    login_url = kite.login_url()
    return Response(
        status_code=302,
        headers={"Location": login_url},
    )


@app.get("/kite/callback")
async def kite_callback(status: str = "unknown", request_token: str = "") -> Response:
    if status != "success" or not request_token:
        raise HTTPException(
            status_code=400,
            detail=f"Kite callback failed: status={status!r}",
        )
    try:
        access_token = get_session_manager().handle_callback(request_token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {exc}")
    settings = get_settings()
    if _watcher is not None:
        _watcher.restart(api_key=settings.KITE_API_KEY, access_token=access_token)
    # Validate the fresh token immediately so SESSION_INVALID is cleared and
    # checked_at reflects the actual login time, not the stale startup check.
    daily_session_check()
    return Response(
        content="<html><body><h2>Login complete. You may close this window.</h2></body></html>",
        media_type="text/html",
    )


@app.get("/healthz")
async def healthz(settings: Settings = Depends(get_current_settings)) -> dict:
    token_age_hours = None
    try:
        result = get_session_manager()._load_token()
        if result is not None:
            _, created_at = result
            age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
            token_age_hours = round(age.total_seconds() / 3600, 2)
    except Exception:
        pass
    return {"status": "ok", "token_age_hours": token_age_hours, "dry_run": settings.DRY_RUN}


# — PWA assets (no auth required) ——————————————————————————————————————————

def _make_icon_png(size: int) -> bytes:
    """Generate a solid Apple-blue PNG icon using only stdlib (no Pillow)."""
    import struct, zlib as _zlib
    r, g, b = 0x00, 0x7A, 0xFF
    row = bytes([0] + [r, g, b] * size)
    raw = row * size
    compressed = _zlib.compress(raw, 9)
    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", _zlib.crc32(body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


@app.get("/manifest.json")
async def pwa_manifest() -> Response:
    import json as _json
    manifest = {
        "name": "ZeroBot",
        "short_name": "ZeroBot",
        "description": "Zerodha Algo Trading Bot",
        "start_url": "/control",
        "display": "standalone",
        "background_color": "#F2F2F7",
        "theme_color": "#007AFF",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(content=_json.dumps(manifest), media_type="application/manifest+json")


@app.get("/icon-192.png")
async def icon_192() -> Response:
    return Response(content=_make_icon_png(192), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon-512.png")
async def icon_512() -> Response:
    return Response(content=_make_icon_png(512), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


_SW_JS = (
    "const CACHE='zerobot-v3';"
    "const SHELL=['/control','/orders','/gtts','/history','/dashboard'];"
    "self.addEventListener('install',e=>{"
    "e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL).catch(()=>{})));"
    "self.skipWaiting()});"
    "self.addEventListener('activate',e=>{"
    "e.waitUntil(caches.keys().then(keys=>Promise.all("
    "keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));"
    "self.clients.claim()});"
    "self.addEventListener('fetch',e=>{"
    "if(e.request.method!=='GET')return;"
    "e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)))});"
)


@app.get("/sw.js")
async def service_worker() -> Response:
    return Response(
        content=_SW_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store"},
    )


_CSS = (
    # ── Reset ────────────────────────────────────────────────────────────────
    "*{box-sizing:border-box;margin:0;padding:0}"

    # ── Base ─────────────────────────────────────────────────────────────────
    "body{"
    "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif;"
    "background:#F2F2F7;color:#1C1C1E;min-height:100vh;"
    "-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale"
    "}"

    # ── Header — frosted glass, sticky ───────────────────────────────────────
    ".hdr{"
    "background:rgba(255,255,255,.88);"
    "backdrop-filter:saturate(180%) blur(20px);"
    "-webkit-backdrop-filter:saturate(180%) blur(20px);"
    "border-bottom:.5px solid rgba(0,0,0,.1);"
    "padding:12px 20px;display:flex;align-items:center;gap:12px;"
    "position:sticky;top:0;z-index:100"
    "}"
    ".hdr-icon{"
    "width:34px;height:34px;"
    "background:linear-gradient(145deg,#007AFF,#5856D6);"
    "border-radius:9px;display:flex;align-items:center;justify-content:center;"
    "font-size:1.05em;box-shadow:0 2px 8px rgba(0,122,255,.28);flex-shrink:0"
    "}"
    ".hdr-t{color:#1C1C1E;font-size:1.02em;font-weight:700;letter-spacing:-.02em}"
    ".hdr-s{color:#8E8E93;font-size:.72em;margin-top:1px;font-weight:400}"

    # ── Nav — frosted, blue active indicator ─────────────────────────────────
    ".nav{"
    "background:rgba(255,255,255,.92);"
    "backdrop-filter:saturate(180%) blur(20px);"
    "-webkit-backdrop-filter:saturate(180%) blur(20px);"
    "display:flex;padding:0 6px;"
    "border-bottom:.5px solid rgba(0,0,0,.08);"
    "overflow-x:auto;scrollbar-width:none;-ms-overflow-style:none"
    "}"
    ".nav::-webkit-scrollbar{display:none}"
    ".nav a{"
    "color:#636366;text-decoration:none;padding:10px 14px;"
    "font-size:.8em;font-weight:500;"
    "border-bottom:2px solid transparent;"
    "transition:color .2s,border-color .2s;"
    "white-space:nowrap;letter-spacing:-.01em"
    "}"
    ".nav a:hover{color:#007AFF}"
    ".nav a.on{color:#007AFF;border-bottom-color:#007AFF;font-weight:600}"

    # ── Layout ────────────────────────────────────────────────────────────────
    ".wrap{padding:16px;margin:0 auto}"
    ".wrap-sm{max-width:560px}.wrap-lg{max-width:960px}"

    # ── Cards — iOS grouped list style ───────────────────────────────────────
    ".card{"
    "background:#fff;border-radius:12px;margin-bottom:16px;"
    "overflow:hidden;"
    "box-shadow:0 1px 3px rgba(0,0,0,.07),0 1px 2px rgba(0,0,0,.04)"
    "}"

    # ── Card section header ───────────────────────────────────────────────────
    ".ct{"
    "font-size:.68em;font-weight:600;text-transform:uppercase;letter-spacing:.08em;"
    "color:#8E8E93;padding:12px 16px 10px;"
    "border-bottom:.5px solid rgba(0,0,0,.06);"
    "display:flex;align-items:center;gap:6px"
    "}"
    ".ct::before{content:'';display:block;width:3px;height:12px;border-radius:2px;background:#007AFF}"

    # ── Status pills ──────────────────────────────────────────────────────────
    ".pill{"
    "display:inline-flex;align-items:center;"
    "padding:3px 9px;border-radius:20px;"
    "font-size:.72em;font-weight:600;letter-spacing:.01em"
    "}"
    ".pg{background:rgba(52,199,89,.12);color:#248A3D}"
    ".pr{background:rgba(255,59,48,.12);color:#D70015}"
    ".pa{background:rgba(255,149,0,.12);color:#C93400}"
    ".pb{background:rgba(0,122,255,.1);color:#0040DD}"
    ".pm{background:rgba(142,142,147,.12);color:#636366}"
    ".pp{background:rgba(88,86,214,.1);color:#3634A3}"

    # ── Progress bar rows ─────────────────────────────────────────────────────
    ".mr{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:.5px solid rgba(0,0,0,.05)}"
    ".mr:last-of-type{border-bottom:none}"
    ".ml{font-size:.78em;color:#636366;width:130px;flex-shrink:0;font-weight:500}"
    ".mw{flex:1;height:6px;background:#E5E5EA;border-radius:3px;overflow:hidden}"
    ".mb{height:100%;border-radius:3px;transition:width .5s cubic-bezier(.25,.46,.45,.94)}"
    ".mv{font-size:.78em;font-weight:700;min-width:95px;text-align:right;font-variant-numeric:tabular-nums}"

    # ── Value colours ─────────────────────────────────────────────────────────
    ".ok{color:#248A3D}.wn{color:#C93400}.bd{color:#D70015}"
    ".bok{background:#34C759}.bwn{background:#FF9500}.bbd{background:#FF3B30}"

    # ── Settings rows (iOS-style) ─────────────────────────────────────────────
    ".mdr{"
    "display:flex;align-items:center;justify-content:space-between;"
    "padding:12px 16px;border-bottom:.5px solid rgba(0,0,0,.06)"
    "}"
    ".mdr:last-of-type{border-bottom:none}"
    ".mdl{font-size:.84em;color:#1C1C1E;font-weight:500;display:flex;align-items:center;gap:8px}"

    # ── Buttons ───────────────────────────────────────────────────────────────
    ".btn{"
    "padding:8px 16px;border:none;border-radius:8px;"
    "font-size:.79em;font-weight:600;cursor:pointer;"
    "transition:opacity .15s,transform .1s;color:#fff;"
    "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif;"
    "letter-spacing:-.01em;display:inline-flex;align-items:center;justify-content:center"
    "}"
    ".btn:active{transform:scale(.96)}"
    ".btn:hover{opacity:.88}"
    ".bn{background:#1C1C1E}"
    ".bg2{background:#34C759}"
    ".br2{background:#FF3B30}"
    ".ba{background:#FF9500}"
    ".bp{background:#007AFF}"
    ".bm{background:#E5E5EA;color:#3A3A3C}"
    ".bfull{"
    "display:block;width:calc(100% - 32px);margin:12px 16px 16px;"
    "padding:14px;font-size:.9em;text-align:center;border-radius:10px"
    "}"

    # ── Emergency stop banner ─────────────────────────────────────────────────
    ".sbanner{"
    "background:linear-gradient(135deg,#FF3B30,#D70015);"
    "color:#fff;padding:14px 16px;border-radius:12px;"
    "font-weight:700;text-align:center;"
    "margin-bottom:16px;font-size:.88em;letter-spacing:.01em;"
    "box-shadow:0 4px 16px rgba(255,59,48,.35)"
    "}"

    # ── Risk param rows ───────────────────────────────────────────────────────
    ".pr2{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:.5px solid rgba(0,0,0,.05)}"
    ".pr2:last-of-type{border-bottom:none}"
    ".pl{flex:1;font-size:.82em;color:#3A3A3C;font-weight:500}"
    ".pi{"
    "width:90px;padding:7px 10px;"
    "border:1px solid #E5E5EA;border-radius:8px;"
    "font-size:.84em;text-align:right;background:#F9F9F9;color:#1C1C1E;"
    "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif;"
    "transition:border-color .2s,box-shadow .2s"
    "}"
    ".pi:focus{outline:none;border-color:#007AFF;background:#fff;box-shadow:0 0 0 3px rgba(0,122,255,.15)}"
    ".pu{font-size:.74em;color:#8E8E93;width:26px;text-align:left}"

    # ── Override dot ──────────────────────────────────────────────────────────
    ".od{display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF9500;margin-left:4px;vertical-align:middle}"

    # ── Tables ────────────────────────────────────────────────────────────────
    "table{width:100%;border-collapse:collapse}"
    "th{"
    "font-size:.68em;font-weight:600;text-transform:uppercase;letter-spacing:.07em;"
    "color:#8E8E93;padding:10px 14px;"
    "border-bottom:.5px solid rgba(0,0,0,.08);background:#FAFAFA;text-align:left"
    "}"
    "td{padding:10px 14px;font-size:.82em;border-bottom:.5px solid rgba(0,0,0,.05);color:#1C1C1E}"
    "tr:last-child td{border-bottom:none}"
    "tbody tr:hover td{background:#F9F9FB}"
    ".tc{text-align:center}.tr{text-align:right}"
)

# ── Quick Trade panel — injected at top of /control ───────────────────────────
_QUICK_TRADE_PANEL = """
<div class="card" id="qt-panel">
<div class="ct">Quick Trade &#x26A1;</div>
<div id="qt-input-area" style="padding:16px">
<textarea id="qt-text" rows="2" style="width:100%;font-size:18px;padding:12px 14px;border:1.5px solid #E5E5EA;border-radius:10px;resize:none;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif;background:#FAFAFA;color:#1C1C1E;-webkit-appearance:none;transition:border-color .2s;margin-bottom:10px;display:block" placeholder="Type or dictate your trade &#x2014; e.g. buy one lot NIFTY ATM call"></textarea>
<div id="qt-err" style="display:none;background:rgba(255,59,48,.08);color:#D70015;border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:10px"></div>
<button id="qt-parse-btn" class="btn bp bfull">Parse Trade</button>
</div>
<div id="qt-confirm" style="display:none;padding:16px;border-top:.5px solid rgba(0,0,0,.08)">
<div id="qt-summary" style="font-size:17px;font-weight:600;color:#1C1C1E;line-height:1.5;margin-bottom:8px"></div>
<div id="qt-confidence" style="font-size:.82em;color:#636366;margin-bottom:10px"></div>
<div id="qt-low-conf-warn" style="display:none;background:rgba(255,149,0,.1);color:#C93400;border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px">&#x26A0;&#xFE0F; Low confidence parse &#x2014; review carefully before executing</div>
<div id="qt-double-warn" style="display:none;background:rgba(255,59,48,.08);color:#D70015;border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px">&#x26A0;&#xFE0F; EXIT / SQUARE-OFF detected &#x2014; this will close positions. Double-check.</div>
<div id="qt-limit-info" style="display:none;background:rgba(0,122,255,.08);color:#0062CC;border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px"></div>
<div id="qt-straddle-report" style="display:none;background:#F2F2F7;border-radius:8px;padding:10px 12px;font-size:.78em;font-family:'SF Mono',Menlo,Consolas,monospace;white-space:pre-wrap;color:#1C1C1E;margin-bottom:8px;line-height:1.55"></div>
<div id="qt-straddle-warn" style="display:none;background:rgba(255,149,0,.1);color:#C93400;border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px">&#x26A0;&#xFE0F; Spread threshold exceeded &#x2014; tap Force Execute to proceed anyway, or Cancel.</div>
<div id="qt-confirm-err" style="display:none;background:rgba(255,59,48,.08);color:#D70015;border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px"></div>
<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
<button id="qt-exec-btn" class="btn bg2" style="flex:1;min-width:130px;padding:14px;font-size:.9em">Execute Trade</button>
<button id="qt-cancel-btn" class="btn bm" style="flex:1;min-width:100px;padding:14px;font-size:.9em">Cancel</button>
</div>
<div style="display:flex;align-items:center;justify-content:space-between;min-height:28px">
<div id="qt-timer" style="font-size:.76em;color:#8E8E93;font-variant-numeric:tabular-nums"></div>
<button id="qt-reparse-btn" class="btn bn" style="display:none;font-size:.76em;padding:6px 12px">Re-parse &#x21BA;</button>
</div>
</div>
<div id="qt-result" style="display:none;padding:16px;border-top:.5px solid rgba(0,0,0,.08)"></div>
<div style="border-top:.5px solid rgba(0,0,0,.08);padding:12px 16px;display:flex;align-items:center;justify-content:space-between">
<div id="qt-channel-status" style="font-size:.8em;color:#8E8E93">Checking channel&#x2026;</div>
<button id="qt-toggle-channel-btn" class="btn bg2" style="font-size:.76em;padding:6px 14px;display:none">Enable Channel</button>
</div>
</div>
<div class="card" id="qt-log-card">
<div class="ct">Quick Trade Log &#x2014; This Session</div>
<div style="overflow-x:auto">
<table><thead><tr><th>Time</th><th>Transcript</th><th>Summary</th><th>Conf.</th><th>Action</th><th>Result</th></tr></thead>
<tbody id="qt-log-tbody"><tr><td colspan="6" style="text-align:center;color:#aaa;font-style:italic">No activity this session</td></tr></tbody></table>
</div>
</div>
<div class="card" id="qt-pending-card">
<div class="ct">Open Limit Orders</div>
<div style="overflow-x:auto">
<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Limit &#x20B9;</th><th>Action</th></tr></thead>
<tbody id="qt-pending-tbody"><tr><td colspan="6" style="text-align:center;color:#aaa;font-style:italic">No open limit orders</td></tr></tbody></table>
</div>
</div>
<script>
(function(){
'use strict';
var st={token:null,transcript:null,expiresAt:null,timer:null,log:[],straddleForce:false};
function el(i){return document.getElementById(i);}

function showErr(msg){var e=el('qt-err');e.textContent=msg;e.style.display='block';}
function hideErr(){el('qt-err').style.display='none';}

el('qt-parse-btn').addEventListener('click',parseTrade);
el('qt-text').addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();parseTrade();}
});

function parseTrade(){
  var text=el('qt-text').value.trim();
  if(!text){showErr('Please enter a trade command');return;}
  hideErr();
  setParseLoading(true);
  fetch('/control/voice/proxy/transcribe',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text})
  }).then(function(r){
    return r.json().then(function(d){return{ok:r.ok,status:r.status,data:d};});
  }).then(function(res){
    if(res.status===401){window.location='/auth/login';return;}
    if(res.status===403){showErr('Voice channel disabled on server');return;}
    if(!res.ok){showErr((res.data&&res.data.detail)||'Server error — please try again');return;}
    var d=res.data;
    st.token=d.confirmation_token;
    st.transcript=el('qt-text').value.trim();
    st.expiresAt=Date.now()+d.expires_in_seconds*1000;
    showConfirm(d);
  }).catch(function(){
    showErr('Network error — please try again');
  }).finally(function(){setParseLoading(false);});
}

function setParseLoading(on){
  var b=el('qt-parse-btn');
  b.disabled=on;b.textContent=on?'⏳ Parsing…':'Parse Trade';
  el('qt-input-area').style.opacity=on?'0.5':'1';
}

function pausePageRefresh(){
  var m=document.querySelector('meta[http-equiv="refresh"]');
  if(m)m.setAttribute('content','');
}
function resumePageRefresh(){
  var m=document.querySelector('meta[http-equiv="refresh"]');
  if(m)m.setAttribute('content','120');
}

function showConfirm(d){
  pausePageRefresh();
  el('qt-input-area').style.display='none';
  el('qt-summary').textContent=d.summary;
  el('qt-confidence').textContent='Confidence: '+Math.round(d.confidence*100)+'%';
  el('qt-low-conf-warn').style.display=d.low_confidence?'block':'none';
  el('qt-double-warn').style.display=d.double_confirm_required?'block':'none';
  var _li=el('qt-limit-info');
  if(_li){var _lp=d.limit_price;if(_lp){var _ls='₹'+_lp+' limit';if(d.estimated_sl)_ls+=' · Est.SL: ₹'+d.estimated_sl;if(d.estimated_target)_ls+=' · Est.Target: ₹'+d.estimated_target;_li.textContent=_ls;_li.style.display='block';}else{_li.style.display='none';}}
  el('qt-confirm-err').style.display='none';
  var _sr=el('qt-straddle-report'),_sw=el('qt-straddle-warn');
  if(_sr){if(d.straddle_report){_sr.textContent=d.straddle_report;_sr.style.display='block';}else{_sr.style.display='none';}}
  if(_sw){_sw.style.display=(d.straddle_spread_warn?'block':'none');}
  st.straddleForce=false;
  var _execBtn=el('qt-exec-btn');
  if(d.straddle_spread_warn){
    _execBtn.textContent='Force Execute';_execBtn.style.background='#FF9500';
    st.straddleForce=true;
  }else{
    _execBtn.textContent='Execute Trade';_execBtn.style.background='';
  }
  _execBtn.disabled=false;
  el('qt-cancel-btn').disabled=false;
  el('qt-reparse-btn').style.display='none';
  el('qt-confirm').style.display='block';
  el('qt-result').style.display='none';
  startTimer(d.expires_in_seconds);
}

function startTimer(secs){
  clearInterval(st.timer);
  st.timer=setInterval(function(){
    var rem=Math.max(0,Math.ceil((st.expiresAt-Date.now())/1000));
    el('qt-timer').textContent=rem>0?'Expires in '+rem+'s':'Expired — please re-parse';
    if(rem===0){
      clearInterval(st.timer);
      el('qt-exec-btn').disabled=true;el('qt-exec-btn').textContent='Expired';
      el('qt-reparse-btn').style.display='inline-flex';
      resumePageRefresh();
    }
  },500);
}

el('qt-exec-btn').addEventListener('click',function(){
  if(!st.token)return;
  setBothLoading(true);
  fetch('/control/voice/proxy/confirm',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({confirmation_token:st.token,approved:true,force_spread:!!st.straddleForce})
  }).then(function(r){
    return r.json().then(function(d){return{ok:r.ok,status:r.status,data:d};});
  }).then(function(res){
    if(res.status===401){window.location='/auth/login';return;}
    if(!res.ok){
      var e=el('qt-confirm-err');
      e.textContent='⚠ '+((res.data&&res.data.detail)||'Execution failed — try again');
      e.style.display='block';return;
    }
    clearInterval(st.timer);
    showResult('ok',res.data);addLog('Executed',res.data);
    loadPendingOrders();
    setTimeout(resetUI,3000);
  }).catch(function(){
    var e=el('qt-confirm-err');
    e.textContent='⚠ Network error — please try again';e.style.display='block';
  }).finally(function(){setBothLoading(false);});
});

el('qt-cancel-btn').addEventListener('click',function(){
  clearInterval(st.timer);
  if(st.token){
    fetch('/control/voice/proxy/cancel',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirmation_token:st.token})
    }).catch(function(){});
  }
  showResult('cancel',null);addLog('Cancelled',null);
  setTimeout(resetUI,2000);
});

el('qt-reparse-btn').addEventListener('click',function(){resetUI();setTimeout(parseTrade,50);});

var _toggleBtn=el('qt-toggle-channel-btn');
if(_toggleBtn)_toggleBtn.addEventListener('click',function(){
  fetch('/control/voice/proxy/toggle',{method:'POST',headers:{'Content-Type':'application/json'}})
    .then(function(r){return r.ok?r.json():null;})
    .then(function(d){if(d)updateChannelStatus(d.voice_channel_enabled);})
    .catch(function(){});
});

function setBothLoading(on){
  el('qt-exec-btn').disabled=on;el('qt-cancel-btn').disabled=on;
  if(on)el('qt-exec-btn').textContent='⏳ Executing…';
}

function showResult(type,data){
  el('qt-confirm').style.display='none';
  var e=el('qt-result');e.style.display='block';
  resumePageRefresh();
  if(type==='ok'){
    var lines=[];
    if(data&&data.status)lines.push(data.status);
    if(data&&data.result){
      var r=data.result;
      if(r.alert_id)lines.push('Alert #'+r.alert_id);
      if(r.order_id)lines.push('Order #'+r.order_id);
      if(r.dry_run!==undefined)lines.push(r.dry_run?'Paper 📝':'Live order');
    }
    e.innerHTML='<div style="background:rgba(52,199,89,.1);border-radius:10px;padding:14px;color:#248A3D;font-weight:600">'
      +'✅ Trade Executed'
      +(lines.length?'<div style="font-weight:400;font-size:.82em;margin-top:5px;color:#3A3A3C">'+lines.join(' · ')+'</div>':'')
      +'</div>';
  }else if(type==='cancel'){
    e.innerHTML='<div style="background:#F2F2F7;border-radius:10px;padding:14px;color:#636366;font-weight:500">↩ Trade Cancelled</div>';
  }else{
    e.innerHTML='<div style="background:rgba(255,59,48,.08);border-radius:10px;padding:14px;color:#D70015;font-weight:500">⚠ '+(data||'Error')+'</div>';
  }
}

function resetUI(){
  clearInterval(st.timer);st.token=null;st.expiresAt=null;st.straddleForce=false;
  el('qt-input-area').style.display='block';el('qt-input-area').style.opacity='1';
  el('qt-confirm').style.display='none';el('qt-result').style.display='none';
  var _eb=el('qt-exec-btn');_eb.disabled=false;_eb.textContent='Execute Trade';_eb.style.background='';
  el('qt-cancel-btn').disabled=false;
  el('qt-reparse-btn').style.display='none';
  el('qt-confirm-err').style.display='none';
  var _sr=el('qt-straddle-report');if(_sr)_sr.style.display='none';
  var _sw=el('qt-straddle-warn');if(_sw)_sw.style.display='none';
  hideErr();resumePageRefresh();
}

function addLog(action,data){
  var summary=el('qt-summary').textContent,conf=el('qt-confidence').textContent;
  var result='--';
  if(data&&data.status)result=data.status;
  st.log.unshift({
    ts:new Date().toLocaleTimeString(),
    transcript:(st.transcript||'').slice(0,40),
    summary:summary.slice(0,50)+(summary.length>50?'…':''),
    conf:conf,action:action,result:result.slice(0,30)
  });
  if(st.log.length>10)st.log.pop();
  renderLog();
}

function renderLog(){
  var tb=el('qt-log-tbody');if(!tb)return;
  if(!st.log.length){
    tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#aaa;font-style:italic">No activity this session</td></tr>';return;
  }
  tb.innerHTML=st.log.map(function(e){
    var cls=e.action==='Executed'?'pg':e.action==='Cancelled'?'pm':'pr';
    var esc=function(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');};
    return '<tr>'
      +'<td style="white-space:nowrap">'+esc(e.ts)+'</td>'
      +'<td style="max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(e.transcript)+'">'+esc(e.transcript)+'</td>'
      +'<td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(e.summary)+'">'+esc(e.summary)+'</td>'
      +'<td>'+esc(e.conf)+'</td>'
      +'<td><span class="pill '+cls+'">'+e.action+'</span></td>'
      +'<td style="max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(e.result)+'</td>'
      +'</tr>';
  }).join('');
}

function updateChannelStatus(enabled){
  var e=el('qt-channel-status'),b=el('qt-toggle-channel-btn');if(!e)return;
  e.textContent='Channel: '+(enabled?'🟢 Enabled':'🔴 Disabled');
  e.style.color=enabled?'#248A3D':'#D70015';
  if(b){b.textContent=enabled?'Disable Channel':'Enable Channel';b.style.display='inline-flex';}
}

function checkChannel(){
  var e=el('qt-channel-status');if(!e)return;
  fetch('/control/voice/proxy/status')
    .then(function(r){return r.ok?r.json():null;})
    .then(function(d){
      if(!d){e.textContent='Channel status: unavailable';e.style.color='#8E8E93';return;}
      updateChannelStatus(d.voice_channel_enabled);
    }).catch(function(){e.textContent='Channel status: unavailable';e.style.color='#8E8E93';});
}

function loadPendingOrders(){
  fetch('/control/voice/proxy/pending-orders')
    .then(function(r){return r.ok?r.json():null;})
    .then(function(d){if(d)renderPendingOrders(d.pending_limit_orders||[]);})
    .catch(function(){});
}
function renderPendingOrders(orders){
  var tb=el('qt-pending-tbody');if(!tb)return;
  var esc=function(s){return String(s===null||s===undefined?'--':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');};
  if(!orders.length){
    tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#aaa;font-style:italic">No open limit orders</td></tr>';return;
  }
  tb.innerHTML=orders.map(function(o){
    var side_cls=o.transaction_type==='BUY'?'pg':'pr';
    var btn_lbl=o.dry_run?'Cancel (paper)':'Cancel';
    return '<tr>'
      +'<td style="white-space:nowrap">'+esc(o.placed_at)+'</td>'
      +'<td style="font-weight:600">'+esc(o.tradingsymbol)+'</td>'
      +'<td><span class="pill '+side_cls+'">'+esc(o.transaction_type)+'</span></td>'
      +'<td class="tr">'+esc(o.quantity)+'</td>'
      +'<td class="tr">'+esc(o.price)+'</td>'
      +'<td><button class="btn bm" style="font-size:.72em;padding:4px 10px" onclick="window._qtCancelLimitOrder('+o.id+',this)">'+btn_lbl+'</button></td>'
      +'</tr>';
  }).join('');
}
window._qtCancelLimitOrder=function(orderId,btn){
  btn.disabled=true;btn.textContent='Cancelling…';
  fetch('/control/voice/proxy/cancel-order',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({order_id:orderId})
  }).then(function(r){return r.json().then(function(d){return{ok:r.ok,data:d};});})
  .then(function(res){
    if(res.ok){loadPendingOrders();}
    else{btn.disabled=false;btn.textContent='Error';setTimeout(function(){btn.textContent='Cancel';btn.disabled=false;},2000);}
  }).catch(function(){btn.disabled=false;btn.textContent='Cancel';});
};

checkChannel();renderLog();loadPendingOrders();
})();
</script>
"""


def _shell(active: str, content: str, wide: bool = False, refresh: bool = False) -> str:
    """Wrap page content in the shared header / nav / CSS."""
    pages = [("control","Control"),("orders","Orders"),("gtts","GTTs"),
             ("history","History"),("dashboard","Alerts")]
    nav = "".join(
        "<a href='/" + p + "'" + (" class='on'" if p == active else "") + ">" + lbl + "</a>"
        for p, lbl in pages
    )
    wrap_cls = "wrap wrap-lg" if wide else "wrap wrap-sm"
    refresh_meta = "<meta http-equiv='refresh' content='120'>" if refresh else ""
    lut_html = (
        "<span id='lut' style='font-size:.68em;color:rgba(255,255,255,.35);"
        "margin-left:auto;padding:10px 14px;white-space:nowrap'></span>"
        if refresh else ""
    )
    lut_js = (
        "<script>window.addEventListener('load',function(){"
        "var e=document.getElementById('lut');"
        "if(e)e.textContent='Updated '+new Date().toLocaleTimeString();})"
        "</script>"
        if refresh else ""
    )
    return (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ZeroBot</title>"
        + refresh_meta +
        "<link rel='manifest' href='/manifest.json'>"
        "<meta name='theme-color' content='#007AFF'>"
        "<meta name='apple-mobile-web-app-capable' content='yes'>"
        "<meta name='apple-mobile-web-app-status-bar-style' content='default'>"
        "<meta name='apple-mobile-web-app-title' content='ZeroBot'>"
        "<link rel='apple-touch-icon' href='/icon-192.png'>"
        "<style>" + _CSS + "</style>"
        "<script>if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js')</script>"
        "</head><body>"
        "<div class='hdr'>"
        "<div class='hdr-icon'>&#x1F4C8;</div>"
        "<div><div class='hdr-t'>ZeroBot</div><div class='hdr-s'>Zerodha Algo Trading</div></div>"
        "</div>"
        "<div class='nav'>" + nav + lut_html
        + "<a href='/auth/logout' style='margin-left:auto;color:#FF3B30;"
        "font-size:.78em;padding:10px 14px;text-decoration:none;"
        "white-space:nowrap;font-weight:500'>Sign Out</a></div>"
        "<div class='" + wrap_cls + "'>" + content + "</div>"
        + lut_js +
        "</body></html>"
    )


@app.get("/dashboard")
async def dashboard(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    days: int = Query(default=2, ge=0),
) -> Response:
    q = session.query(Alert)
    if days > 0:
        q = q.filter(Alert.received_at >= datetime.now(IST) - timedelta(days=days))
    rows = q.order_by(Alert.received_at.desc()).limit(200).all()

    day_opts = [(1, "Today"), (2, "2 Days"), (7, "7 Days"), (0, "All")]
    filters = "<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px'>" + "".join(
        f"<a href='/dashboard?days={d}' class='btn {'bp' if d == days else 'bm'}' "
        f"style='text-decoration:none'>{lbl}</a>"
        for d, lbl in day_opts
    ) + "</div>"

    rows_html = "".join(
        f"<tr><td>{r.id}</td><td style='font-weight:600'>{r.tv_ticker}</td><td>{r.action}</td>"
        f"<td>{r.received_at.strftime('%m/%d %H:%M') if r.received_at else '—'}</td>"
        f"<td><span class='pill {'pg' if r.processed else 'pa'}'>"
        f"{'YES' if r.processed else 'PENDING'}</span></td></tr>"
        for r in rows
    )
    return Response(
        content=_shell("dashboard",
            filters
            + "<div class='card'><div class='ct'>Recent Alerts</div>"
            "<table><thead><tr><th>ID</th><th>Ticker</th><th>Action</th>"
            "<th>Time</th><th>Processed</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/positions")
async def positions(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
) -> Response:
    rows = session.query(Position).all()
    rows_html = "".join(
        f"<tr><td>{r.id}</td><td style='font-weight:600'>{r.tradingsymbol}</td>"
        f"<td class='tr'>{r.quantity}</td><td class='tr'>&#x20B9;{r.entry_premium:.2f}</td></tr>"
        for r in rows
    )
    return Response(
        content=_shell("",
            "<div class='card'><div class='ct'>Open Positions</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th>"
            "<th class='tr'>Qty</th><th class='tr'>Entry Premium</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/history")
async def history(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    days: int = Query(default=1, ge=0),
) -> Response:
    q = session.query(ClosedTrade)
    if days > 0:
        q = q.filter(ClosedTrade.closed_at >= datetime.now(IST) - timedelta(days=days))
    rows = q.order_by(ClosedTrade.closed_at.desc()).limit(200).all()

    total_pnl = sum((r.pnl or 0) for r in rows)
    pnl_vc = "ok" if total_pnl >= 0 else "bd"

    day_opts = [(1, "Today"), (3, "3 Days"), (7, "7 Days"), (0, "All")]
    filters = "<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px'>" + "".join(
        f"<a href='/history?days={d}' class='btn {'bp' if d == days else 'bm'}' "
        f"style='text-decoration:none'>{lbl}</a>"
        for d, lbl in day_opts
    ) + "</div>"

    summary = (
        f"<div class='card' style='display:flex;gap:16px;align-items:center;padding:11px 18px;margin-bottom:14px'>"
        f"<span style='font-size:.78em;color:#8492a6'>Period P&amp;L</span>"
        f"<span class='mv {pnl_vc}' style='font-size:.95em'>&#x20B9;{total_pnl:+.2f}</span>"
        f"<span style='font-size:.78em;color:#8492a6;margin-left:auto'>{len(rows)} trade(s)</span>"
        f"</div>"
    )

    rows_html = "".join(
        f"<tr><td>{r.id}</td><td style='font-weight:600'>{r.tradingsymbol}</td>"
        f"<td class='tr {'ok' if (r.pnl or 0) >= 0 else 'bd'}'>&#x20B9;{(r.pnl or 0):+.2f}</td>"
        f"<td>{r.exit_reason}</td>"
        f"<td>{r.closed_at.strftime('%m/%d %H:%M') if r.closed_at else '—'}</td></tr>"
        for r in rows
    )
    return Response(
        content=_shell("history",
            filters + summary
            + "<div class='card'><div class='ct'>Trade History</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th><th class='tr'>PnL</th>"
            "<th>Reason</th><th>Closed</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/gtts")
async def gtts_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    show_all: int = Query(default=0, ge=0, le=1),
    days: int = Query(default=2, ge=0),
) -> Response:
    """Show GTT rows alongside live Kite status. Default: ACTIVE + last 2 days."""
    q = session.query(Gtt).order_by(Gtt.placed_at.desc())
    if not show_all:
        q = q.filter(Gtt.status == "ACTIVE")
    if days > 0:
        q = q.filter(Gtt.placed_at >= datetime.now(IST) - timedelta(days=days))
    rows = q.limit(100).all()

    live_map: dict[int, str] = {}
    if not _dry_run(settings):
        try:
            kite_client = get_session_manager().get_kite()
            for g in kite_client.get_gtts():
                live_map[g["id"]] = g["status"]
        except Exception as exc:
            log.warning("gtts_page: could not fetch live GTTs from Kite: %s", exc)

    def _gtt_pill(status: str) -> str:
        if status == "ACTIVE":
            return "pg"
        if status == "DRY_RUN":
            return "pa"
        return "pr"

    rows_html = "".join(
        "<tr>"
        f"<td>{r.id}</td>"
        f"<td style='font-weight:600'>{r.tradingsymbol}</td>"
        f"<td class='tr bd'>&#x20B9;{r.sl_trigger:.2f}</td>"
        f"<td class='tr ok'>&#x20B9;{r.target_trigger:.2f}</td>"
        f"<td class='tr'>&#x20B9;{r.last_price_at_placement:.2f}</td>"
        f"<td><span class='pill {_gtt_pill(r.status)}'>{r.status}</span></td>"
        f"<td class='tc'>{r.kite_gtt_id or '—'}</td>"
        f"<td class='tc'>{live_map.get(r.kite_gtt_id, 'N/A') if r.kite_gtt_id else '—'}</td>"
        f"<td>{r.placed_at.strftime('%m/%d %H:%M') if r.placed_at else '—'}</td>"
        "</tr>"
        for r in rows
    )
    toggle_lbl = "Show All Statuses" if not show_all else "Active Only"
    toggle_cls = "bm" if not show_all else "bp"
    day_opts = [(1, "Today"), (2, "2 Days"), (7, "7 Days"), (0, "All time")]
    day_btns = "".join(
        f"<a href='/gtts?show_all={show_all}&days={d}' class='btn {'bp' if d == days else 'bm'}' "
        f"style='text-decoration:none'>{lbl}</a>"
        for d, lbl in day_opts
    )
    toggle_bar = (
        f"<div style='display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:14px'>"
        + day_btns
        + f"<a href='/gtts?show_all={1 - show_all}&days={days}' class='btn {toggle_cls}' "
        f"style='text-decoration:none;margin-left:auto'>{toggle_lbl}</a>"
        + f"<span style='font-size:.78em;color:#aaa'>{len(rows)} row(s)</span></div>"
    )
    return Response(
        content=_shell("gtts",
            toggle_bar
            + "<div class='card'><div class='ct'>GTT / Stop-Loss Orders</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th><th class='tr'>SL Trigger</th>"
            "<th class='tr'>Target</th><th class='tr'>Entry Price</th><th>DB Status</th>"
            "<th class='tc'>Kite GTT ID</th><th class='tc'>Kite Live</th><th>Placed At</th>"
            "</tr></thead><tbody>" + rows_html + "</tbody></table>"
            "<p style='font-size:.73em;color:#aaa;margin-top:8px'>"
            "Kite Live: <b>active</b>=SL live | <b>triggered</b>=fired | "
            "<b>N/A</b>=not found (triggered+cleaned up or GTT_FAILED)</p></div>",
            wide=True,
            refresh=True,
        ),
        media_type="text/html",
    )


@app.get("/status")
async def status_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    """Legacy status page — redirects to /control."""
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/trade-mode/toggle")
async def toggle_trade_mode(
    _: None = Depends(_auth_guard),
) -> Response:
    new_mode = state.toggle_trade_mode()
    log.info("Trade mode toggled to %s", new_mode)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/stock-mode/toggle")
async def toggle_stock_mode(
    _: None = Depends(_auth_guard),
) -> Response:
    new_mode = state.toggle_stock_mode()
    log.info("Stock mode toggled to %s", new_mode)
    return Response(status_code=302, headers={"Location": "/control"})


# ── /control — unified dashboard ──────────────────────────────────────────────



@app.get("/control")
async def control_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    from app.storage import AppError

    # ── live state ────────────────────────────────────────────────────────────
    trade_mode = state.get_trade_mode()
    stock_mode = state.get_stock_mode()
    paper = _dry_run(settings)
    estop = state.is_emergency_stop()
    overrides = state.get_all_overrides()

    eff_max_lots        = state.get_max_lots(settings.MAX_LOTS_PER_TRADE)
    eff_max_loss        = state.get_max_daily_loss(settings.MAX_DAILY_LOSS_ABS)
    eff_sl_pct          = state.get_sl_pct(settings.SL_PREMIUM_PCT)
    eff_rr              = state.get_rr_ratio(settings.RR_RATIO)
    eff_profit_target   = state.get_daily_profit_target(settings.DAILY_PROFIT_TARGET)
    eff_sell_ppt        = state.get_sell_options_profit_pct(settings.SELL_OPTIONS_PROFIT_PCT)
    eff_entry_start     = state.get_entry_window_start(settings.ENTRY_WINDOW_START)
    eff_entry_end       = state.get_entry_window_end(settings.ENTRY_WINDOW_END)
    eff_no_expiry       = state.get_no_entry_on_expiry_day(settings.NO_ENTRY_ON_EXPIRY_DAY)
    eff_trailing        = state.is_trailing_enabled()
    eff_max_trades      = state.get_max_trades_per_day(settings.MAX_TRADES_PER_DAY)
    eff_max_positions   = state.get_max_open_positions(settings.MAX_OPEN_POSITIONS)
    eff_capital         = state.get_capital_per_trade(settings.CAPITAL_PER_TRADE)
    eff_consec_limit    = state.get_consecutive_losses_limit(settings.CONSECUTIVE_LOSSES_LIMIT)

    # ── today's risk summary ──────────────────────────────────────────────────
    today_loss = _realised_loss_today(session)
    trades_today = _trades_today_count(session)
    consec = _consecutive_losses(session)
    open_pos = _open_position_count(session)
    loss_pct = (today_loss / eff_max_loss * 100) if eff_max_loss > 0 else 0

    # ── meter helpers ─────────────────────────────────────────────────────────
    def _bar(pct: float) -> tuple[str, str]:
        if pct >= 100:
            return "bbd", "bd"
        if pct >= 60:
            return "bwn", "wn"
        return "bok", "ok"

    loss_pct_c = min(loss_pct, 100)
    loss_bar, loss_vc = _bar(loss_pct)

    trades_pct = (trades_today / eff_max_trades * 100) if eff_max_trades else 0
    trades_bar, trades_vc = _bar(trades_pct)

    pos_pct = (open_pos / eff_max_positions * 100) if eff_max_positions else 0
    pos_bar, pos_vc = _bar(pos_pct)

    consec_pct = (consec / eff_consec_limit * 100) if eff_consec_limit else 0
    consec_bar, consec_vc = _bar(consec_pct)

    # ── badges / buttons ──────────────────────────────────────────────────────
    eff_adx_threshold = state.get_adx_threshold(settings.ADX_THRESHOLD)
    mode_pill    = "pg" if trade_mode == "BUY_OPTIONS" else ("pr" if trade_mode == "SELL_OPTIONS" else "pa")
    mode_display = trade_mode.replace("_", " ")
    stock_pill    = "pa" if stock_mode == "EQUITY" else "pp"
    stock_display = "EQUITY (CNC)" if stock_mode == "EQUITY" else "F&amp;O OPTIONS"
    paper_pill   = "pa" if paper else "pp"
    paper_label  = "PAPER MODE" if paper else "LIVE TRADING"
    paper_lbl    = "Go LIVE" if paper else "Go PAPER"
    paper_cls    = "bg2" if paper else "ba"
    estop_lbl    = "&#x2714; Resume Trading" if estop else "&#x26D4; Emergency Stop"
    estop_cls    = "bg2" if estop else "br2"
    stop_banner  = (
        "<div class='sbanner'>&#x26D4; EMERGENCY STOP ACTIVE &mdash; No new trades will execute</div>"
        if estop else ""
    )
    trail_pill  = "pg" if eff_trailing else "pr"
    trail_label = "TRAIL ON" if eff_trailing else "TRAIL OFF"
    trail_lbl   = "Disable Trailing" if eff_trailing else "Enable Trailing"
    trail_cls   = "ba" if eff_trailing else "bg2"

    def src(key: str) -> str:
        return (
            " <span style='color:#f0a500;font-size:.72em'>(overridden)</span>"
            if overrides.get(key) is not None else ""
        )

    def src_val(key: str, val: object, default: object) -> str:
        return (
            " <span style='color:#f0a500;font-size:.72em'>(overridden)</span>"
            if val != default else ""
        )

    # ── Kite session status ───────────────────────────────────────────────────
    try:
        token_info = get_session_manager().get_token_info()
        sess_valid = token_info["is_valid"]
        sess_age   = token_info.get("age_hours")
        sess_reason = token_info.get("reason") or ""
    except Exception:
        sess_valid = False
        sess_age   = None
        sess_reason = "unavailable"

    checked_at = get_last_checked_at()
    checked_str = (
        "Checked " + checked_at.astimezone(IST).strftime("%H:%M") if checked_at else "Not checked yet"
    )
    if sess_valid:
        sess_pill  = "pg"
        sess_label = f"Valid&ensp;({sess_age:.1f}h old)" if sess_age is not None else "Valid"
    else:
        sess_pill  = "pr"
        sess_label = f"Invalid &mdash; {sess_reason}" if sess_reason else "Invalid"

    # ── errors: last 48 h ─────────────────────────────────────────────────────
    err_cutoff = datetime.now(IST) - timedelta(hours=48)
    errors = (
        session.query(AppError)
        .filter(AppError.occurred_at >= err_cutoff)
        .order_by(AppError.occurred_at.desc())
        .limit(20)
        .all()
    )
    errors_html = "".join(
        f"<tr><td style='color:#8492a6;white-space:nowrap'>"
        f"{e.occurred_at.astimezone(IST).strftime('%m/%d %H:%M')}</td>"
        f"<td>{e.error_type}</td>"
        f"<td style='max-width:260px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis'>"
        f"{e.message[:140]}</td></tr>"
        for e in errors
    ) or "<tr><td colspan='3' style='text-align:center;color:#aaa'>No errors in last 48h</td></tr>"

    body = (
        _QUICK_TRADE_PANEL
        + stop_banner

        # risk summary
        + "<div class='card'><div class='ct'>Today's Risk Summary</div>"
        + f"<div class='mr'><div class='ml'>Daily loss</div>"
        + f"<div class='mw'><div class='mb {loss_bar}' style='width:{loss_pct_c:.0f}%'></div></div>"
        + f"<div class='mv {loss_vc}'>&#x20B9;{today_loss:.0f}&thinsp;/&thinsp;&#x20B9;{eff_max_loss:.0f}</div></div>"
        + f"<div class='mr'><div class='ml'>Trades today</div>"
        + f"<div class='mw'><div class='mb {trades_bar}' style='width:{min(trades_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {trades_vc}'>{trades_today}&thinsp;/&thinsp;{eff_max_trades}</div></div>"
        + f"<div class='mr'><div class='ml'>Open positions</div>"
        + f"<div class='mw'><div class='mb {pos_bar}' style='width:{min(pos_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {pos_vc}'>{open_pos}&thinsp;/&thinsp;{eff_max_positions}</div></div>"
        + f"<div class='mr'><div class='ml'>Consec. losses</div>"
        + f"<div class='mw'><div class='mb {consec_bar}' style='width:{min(consec_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {consec_vc}'>{consec}&thinsp;/&thinsp;{eff_consec_limit}</div></div>"
        + "</div>"

        # kite session
        + "<div class='card'><div class='ct'>Kite Session</div>"
        + f"<div class='mdr'><div class='mdl'>Status&ensp;<span class='pill {sess_pill}'>{sess_label}</span>"
        + f"<span style='font-size:.72em;color:#aaa'>&ensp;{checked_str}</span></div>"
        + "<a href='/kite/login' class='btn bn' style='text-decoration:none'>Re-login</a></div>"
        + "</div>"

        # mode / switches
        + "<div class='card'><div class='ct'>Mode</div>"
        + f"<div class='mdr'><div class='mdl'>Trade Mode&ensp;<span class='pill {mode_pill}'>{mode_display}</span>"
        + ("<span style='font-size:.72em;color:#8E8E93'>&ensp;BUY->SELL CE&ensp;SELL->SELL PE&ensp;(ADX&lt;threshold only)</span>" if trade_mode == "RANGE_SELL" else "")
        + ("<span style='font-size:.72em;color:#8E8E93'>&ensp;BUY->SELL PE&ensp;SELL->SELL CE</span>" if trade_mode == "SELL_OPTIONS" else "")
        + ("<span style='font-size:.72em;color:#8E8E93'>&ensp;BUY->BUY CE&ensp;SELL->BUY PE</span>" if trade_mode == "BUY_OPTIONS" else "")
        + "</div>"
        + "<form method='post' action='/trade-mode/toggle' style='margin:0'>"
        + "<button class='btn bn' type='submit'>Cycle</button></form></div>"
        + f"<div class='mdr'><div class='mdl'>Stocks (non-index)&ensp;<span class='pill {stock_pill}'>{stock_display}</span></div>"
        + "<form method='post' action='/control/stock-mode/toggle' style='margin:0'>"
        + "<button class='btn bn' type='submit'>Toggle</button></form></div>"
        + f"<div class='mdr'><div class='mdl'>Paper / Live&ensp;<span class='pill {paper_pill}'>{paper_label}</span></div>"
        + "<form method='post' action='/control/paper-mode/toggle' style='margin:0'>"
        + f"<button class='btn {paper_cls}' type='submit'>{paper_lbl}</button></form></div>"
        + f"<div class='mdr'><div class='mdl'>Trailing SL&ensp;<span class='pill {trail_pill}'>{trail_label}</span></div>"
        + "<form method='post' action='/control/trailing/toggle' style='margin:0'>"
        + f"<button class='btn {trail_cls}' type='submit'>{trail_lbl}</button></form></div>"
        + "<div style='margin-top:10px'><form method='post' action='/control/emergency-stop/toggle'>"
        + f"<button class='btn bfull {estop_cls}' type='submit'>{estop_lbl}</button>"
        + "</form></div></div>"

        # risk params
        + "<div class='card'><div class='ct'>Risk Parameters</div>"
        + "<form method='post' action='/control/risk'>"
        + f"<div class='pr2'><div class='pl'>Max lots / trade{src('max_lots')}</div>"
        + f"<input class='pi' type='number' name='max_lots' value='{eff_max_lots}' min='1' max='20' step='1'>"
        + "<div class='pu'>lots</div></div>"
        + f"<div class='pr2'><div class='pl'>Max daily loss{src('max_daily_loss')}</div>"
        + f"<input class='pi' type='number' name='max_daily_loss' value='{eff_max_loss:.0f}' min='500' step='500'>"
        + "<div class='pu'>&#x20B9;</div></div>"
        + f"<div class='pr2'><div class='pl'>SL % (options){src('sl_pct')}</div>"
        + f"<input class='pi' type='number' name='sl_pct' value='{eff_sl_pct*100:.1f}' min='1' max='50' step='0.5'>"
        + "<div class='pu'>%</div></div>"
        + f"<div class='pr2'><div class='pl'>R:R ratio{src('rr_ratio')}</div>"
        + f"<input class='pi' type='number' name='rr_ratio' value='{eff_rr:.1f}' min='0.5' max='10' step='0.1'>"
        + "<div class='pu'>&#xD7;</div></div>"
        + f"<div class='pr2'><div class='pl'>Daily profit target{src('daily_profit_target')}</div>"
        + f"<input class='pi' type='number' name='daily_profit_target' value='{eff_profit_target:.0f}' min='0' step='500'>"
        + "<div class='pu'>&#x20B9;</div></div>"
        + f"<div class='pr2'><div class='pl'>SELL options profit %{src('sell_options_profit_pct')}</div>"
        + f"<input class='pi' type='number' name='sell_options_profit_pct' value='{eff_sell_ppt*100:.0f}' min='5' max='95' step='5'>"
        + "<div class='pu'>%</div></div>"
        + f"<div class='pr2'><div class='pl'>Entry window start{src('entry_window_start')}</div>"
        + f"<input class='pi' type='time' name='entry_window_start' value='{eff_entry_start}' style='width:92px'>"
        + "<div class='pu'></div></div>"
        + f"<div class='pr2'><div class='pl'>Entry window end{src('entry_window_end')}</div>"
        + f"<input class='pi' type='time' name='entry_window_end' value='{eff_entry_end}' style='width:92px'>"
        + "<div class='pu'></div></div>"
        + f"<div class='pr2'><div class='pl'>Block SELL on expiry day{src('no_entry_on_expiry_day')}</div>"
        + f"<select class='pi' name='no_entry_on_expiry_day' style='width:86px'>"
        + f"<option value='1'{' selected' if eff_no_expiry else ''}>Yes</option>"
        + f"<option value='0'{' selected' if not eff_no_expiry else ''}>No</option>"
        + "</select><div class='pu'></div></div>"
        + f"<div class='pr2'><div class='pl'>Max trades / day{src_val('max_trades_per_day', eff_max_trades, settings.MAX_TRADES_PER_DAY)}</div>"
        + f"<input class='pi' type='number' name='max_trades_per_day' value='{eff_max_trades}' min='1' max='50' step='1'>"
        + "<div class='pu'>trades</div></div>"
        + f"<div class='pr2'><div class='pl'>Max open positions{src_val('max_open_positions', eff_max_positions, settings.MAX_OPEN_POSITIONS)}</div>"
        + f"<input class='pi' type='number' name='max_open_positions' value='{eff_max_positions}' min='1' max='20' step='1'>"
        + "<div class='pu'>pos</div></div>"
        + f"<div class='pr2'><div class='pl'>Capital / trade{src_val('capital_per_trade', eff_capital, settings.CAPITAL_PER_TRADE)}</div>"
        + f"<input class='pi' type='number' name='capital_per_trade' value='{eff_capital:.0f}' min='1000' step='1000'>"
        + "<div class='pu'>&#x20B9;</div></div>"
        + f"<div class='pr2'><div class='pl'>Consec. losses limit{src_val('consecutive_losses_limit', eff_consec_limit, settings.CONSECUTIVE_LOSSES_LIMIT)}</div>"
        + f"<input class='pi' type='number' name='consecutive_losses_limit' value='{eff_consec_limit}' min='1' max='20' step='1'>"
        + "<div class='pu'>losses</div></div>"
        + f"<div class='pr2'><div class='pl'>ADX threshold (Range Sell){src_val('adx_threshold', eff_adx_threshold, settings.ADX_THRESHOLD)}</div>"
        + f"<input class='pi' type='number' name='adx_threshold' value='{eff_adx_threshold:.1f}' min='5' max='100' step='1'>"
        + "<div class='pu'>ADX</div></div>"
        + "<div style='display:flex;gap:8px;padding:12px 16px 16px'>"
        + "<button class='btn bp' type='submit' style='flex:1'>Apply</button>"
        + "<button class='btn bm' type='submit' name='reset' value='1' style='flex:1'>Reset to defaults</button>"
        + "</div></form></div>"

        # errors
        + "<div class='card'><div class='ct'>Recent Errors</div>"
        + "<table><thead><tr><th>Time</th><th>Type</th><th>Message</th></tr></thead><tbody>"
        + errors_html + "</tbody></table></div>"
    )
    return Response(content=_shell("control", body, refresh=True), media_type="text/html")


@app.post("/control/paper-mode/toggle")
async def toggle_paper_mode(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    current = _dry_run(settings)
    state.set_paper_mode(not current)
    log.info("Paper mode toggled to %s", not current)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/emergency-stop/toggle")
async def toggle_emergency_stop(_: None = Depends(_auth_guard)) -> Response:
    new_val = not state.is_emergency_stop()
    state.set_emergency_stop(new_val)
    log.warning("Emergency stop set to %s", new_val)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/trailing/toggle")
async def toggle_trailing(_: None = Depends(_auth_guard)) -> Response:
    new_val = state.toggle_trailing_enabled()
    log.info("Trailing SL toggled to %s", new_val)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/risk")
async def update_risk(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    reset: str = Form(default=""),
    max_lots: str = Form(default=""),
    max_daily_loss: str = Form(default=""),
    sl_pct: str = Form(default=""),
    rr_ratio: str = Form(default=""),
    daily_profit_target: str = Form(default=""),
    sell_options_profit_pct: str = Form(default=""),
    entry_window_start: str = Form(default=""),
    entry_window_end: str = Form(default=""),
    no_entry_on_expiry_day: str = Form(default=""),
    max_trades_per_day: str = Form(default=""),
    max_open_positions: str = Form(default=""),
    capital_per_trade: str = Form(default=""),
    consecutive_losses_limit: str = Form(default=""),
    adx_threshold: str = Form(default=""),
) -> Response:
    if reset == "1":
        state.set_max_lots(None)
        state.set_max_daily_loss(None)
        state.set_sl_pct(None)
        state.set_rr_ratio(None)
        state.set_daily_profit_target(None)
        state.set_sell_options_profit_pct(None)
        state.set_entry_window_start(None)
        state.set_entry_window_end(None)
        state.set_no_entry_on_expiry_day(None)
        state.set_max_trades_per_day(None)
        state.set_max_open_positions(None)
        state.set_capital_per_trade(None)
        state.set_consecutive_losses_limit(None)
        state.set_adx_threshold(None)
        log.info("Risk params reset to .env defaults")
    else:
        try:
            if max_lots.strip():
                state.set_max_lots(int(max_lots))
            if max_daily_loss.strip():
                state.set_max_daily_loss(float(max_daily_loss))
            if sl_pct.strip():
                state.set_sl_pct(float(sl_pct) / 100.0)
            if rr_ratio.strip():
                state.set_rr_ratio(float(rr_ratio))
            if daily_profit_target.strip():
                state.set_daily_profit_target(float(daily_profit_target))
            if sell_options_profit_pct.strip():
                state.set_sell_options_profit_pct(float(sell_options_profit_pct) / 100.0)
            if entry_window_start.strip():
                state.set_entry_window_start(entry_window_start.strip())
            if entry_window_end.strip():
                state.set_entry_window_end(entry_window_end.strip())
            if no_entry_on_expiry_day.strip():
                state.set_no_entry_on_expiry_day(no_entry_on_expiry_day == "1")
            if max_trades_per_day.strip():
                state.set_max_trades_per_day(int(max_trades_per_day))
            if max_open_positions.strip():
                state.set_max_open_positions(int(max_open_positions))
            if capital_per_trade.strip():
                state.set_capital_per_trade(float(capital_per_trade))
            if consecutive_losses_limit.strip():
                state.set_consecutive_losses_limit(int(consecutive_losses_limit))
            if adx_threshold.strip():
                state.set_adx_threshold(float(adx_threshold))
            log.info(
                "Risk params updated: max_lots=%s max_loss=%s sl_pct=%s rr=%s "
                "profit_target=%s sell_ppt=%s entry_start=%s entry_end=%s no_expiry=%s "
                "max_trades=%s max_pos=%s capital=%s consec=%s",
                max_lots, max_daily_loss, sl_pct, rr_ratio,
                daily_profit_target, sell_options_profit_pct,
                entry_window_start, entry_window_end, no_entry_on_expiry_day,
                max_trades_per_day, max_open_positions, capital_per_trade, consecutive_losses_limit,
            )
        except ValueError:
            pass
    return Response(status_code=302, headers={"Location": "/control"})


# ── Voice proxy — dashboard-auth'd wrappers so browser needs no token ─────────

import httpx as _httpx  # noqa: E402 — already in requirements.txt

_VOICE_BASE = "http://127.0.0.1:8000"


async def _voice_proxy(method: str, path: str, body: bytes, settings) -> Response:
    """Forward a voice API call with server-side token injection."""
    tok = settings.VOICE_AUTH_TOKEN
    if not tok:
        return Response(
            content='{"detail":"VOICE_AUTH_TOKEN not set on server"}',
            status_code=503, media_type="application/json",
        )
    async with _httpx.AsyncClient() as client:
        r = await client.request(
            method, f"{_VOICE_BASE}{path}",
            content=body,
            headers={"Content-Type": "application/json", "X-Voice-Auth-Token": tok},
            timeout=30.0,
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/control/voice/proxy/transcribe")
async def vp_transcribe(
    request: Request,
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    return await _voice_proxy("POST", "/voice/transcribe", await request.body(), settings)


@app.post("/control/voice/proxy/confirm")
async def vp_confirm(
    request: Request,
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    return await _voice_proxy("POST", "/voice/confirm", await request.body(), settings)


@app.post("/control/voice/proxy/cancel")
async def vp_cancel(
    request: Request,
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    return await _voice_proxy("POST", "/voice/cancel", await request.body(), settings)


@app.get("/control/voice/proxy/status")
async def vp_status(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    admin_tok = settings.ADMIN_AUTH_TOKEN
    if not admin_tok:
        return Response(
            content='{"detail":"ADMIN_AUTH_TOKEN not set on server"}',
            status_code=503, media_type="application/json",
        )
    async with _httpx.AsyncClient() as client:
        r = await client.get(
            f"{_VOICE_BASE}/admin/voice/status",
            headers={"X-Admin-Token": admin_tok},
            timeout=10.0,
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/control/voice/proxy/toggle")
async def vp_toggle(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    admin_tok = settings.ADMIN_AUTH_TOKEN
    if not admin_tok:
        return Response(
            content='{"detail":"ADMIN_AUTH_TOKEN not set on server"}',
            status_code=503, media_type="application/json",
        )
    async with _httpx.AsyncClient() as client:
        r = await client.post(
            f"{_VOICE_BASE}/admin/voice/toggle",
            headers={"X-Admin-Token": admin_tok},
            timeout=10.0,
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/control/voice/proxy/pending-orders")
async def vp_pending_orders(
    _: None = Depends(_auth_guard),
    session: Session = Depends(get_db_session),
) -> Response:
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(Order)
        .filter(
            Order.order_type == "LIMIT",
            Order.status == "PENDING",
            Order.placed_at >= today_start,
        )
        .order_by(Order.placed_at.desc())
        .all()
    )
    data = [
        {
            "id": r.id,
            "kite_order_id": r.kite_order_id,
            "tradingsymbol": r.tradingsymbol,
            "transaction_type": r.transaction_type,
            "quantity": r.quantity,
            "price": r.price,
            "placed_at": r.placed_at.strftime("%H:%M:%S") if r.placed_at else "--",
            "dry_run": r.dry_run,
        }
        for r in rows
    ]
    return Response(content=json.dumps({"pending_limit_orders": data}), media_type="application/json")


@app.post("/control/voice/proxy/cancel-order")
async def vp_cancel_order(
    request: Request,
    _: None = Depends(_auth_guard),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    body = await request.json()
    order_id = body.get("order_id")
    if not order_id:
        return Response(content='{"detail":"order_id required"}', status_code=400, media_type="application/json")
    order = session.get(Order, int(order_id))
    if order is None or order.status != "PENDING":
        return Response(
            content='{"detail":"Order not found or not in PENDING status"}',
            status_code=404, media_type="application/json",
        )
    if not _dry_run(settings) and order.kite_order_id:
        try:
            kite_client = get_session_manager().get_kite()
            kite_client.cancel_order(variety=order.variety, order_id=order.kite_order_id)
        except Exception as exc:
            log.warning("vp_cancel_order: kite cancel failed order_id=%s: %s", order_id, exc)
    order.status = "CANCELLED"
    order.updated_at = datetime.now(IST)
    session.commit()
    return Response(
        content=json.dumps({"status": "cancelled", "order_id": order_id}),
        media_type="application/json",
    )


# ── /orders — consolidated trade view ────────────────────────────────────────

@app.get("/orders")
async def orders_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    days: int = Query(default=1, ge=0),
) -> Response:
    q = (
        session.query(Order, Position, Gtt, ClosedTrade)
        .outerjoin(Position, Position.order_id == Order.id)
        .outerjoin(Gtt, Gtt.order_id == Order.id)
        .outerjoin(ClosedTrade, ClosedTrade.position_id == Position.id)
    )
    if days > 0:
        q = q.filter(Order.placed_at >= datetime.now(IST) - timedelta(days=days))
    rows = q.order_by(Order.placed_at.desc()).limit(200).all()

    def pnl_cls(pnl):
        if pnl is None:
            return ""
        return "ok" if pnl >= 0 else "bd"

    def status_badge(order, ct):
        if ct is not None:
            reason = ct.exit_reason or "CLOSED"
            pill_cls = "pg" if (ct.pnl or 0) >= 0 else "pr"
            return f"<span class='pill {pill_cls}'>{reason}</span>"
        s = order.status
        if s == "DRY_RUN":
            return "<span class='pill pa'>PAPER</span>"
        if s in ("PENDING", "COMPLETE"):
            return "<span class='pill pb'>OPEN</span>"
        return f"<span class='pill pm'>{s}</span>"

    # summary stats
    total_pnl  = sum((ct.pnl or 0) for _, _, _, ct in rows if ct is not None)
    open_count = sum(1 for _, pos, _, ct in rows if pos is not None and ct is None)
    closed_count = sum(1 for _, _, _, ct in rows if ct is not None)
    pnl_vc = "ok" if total_pnl >= 0 else "bd"

    day_opts = [(1, "Today"), (3, "3 Days"), (7, "7 Days"), (0, "All")]
    filters = "<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px'>" + "".join(
        f"<a href='/orders?days={d}' class='btn {'bp' if d == days else 'bm'}' "
        f"style='text-decoration:none'>{lbl}</a>"
        for d, lbl in day_opts
    ) + "</div>"

    summary = (
        f"<div class='card' style='display:flex;gap:16px;align-items:center;padding:11px 18px;margin-bottom:14px'>"
        f"<span style='font-size:.78em;color:#8492a6'>Period P&amp;L</span>"
        f"<span class='mv {pnl_vc}' style='font-size:.95em'>&#x20B9;{total_pnl:+.2f}</span>"
        f"<span style='font-size:.78em;color:#8492a6;margin-left:auto'>"
        f"Open: <b>{open_count}</b>&ensp;Closed: <b>{closed_count}</b></span>"
        f"</div>"
    )

    rows_html = ""
    for order, pos, gtt, ct in rows:
        entry_px = f"₹{pos.entry_premium:.2f}" if pos else "—"
        sl       = f"₹{gtt.sl_trigger:.2f}" if gtt else "—"
        tgt      = f"₹{gtt.target_trigger:.2f}" if gtt else "—"
        pnl_val  = ct.pnl if ct else None
        pnl_str  = f"₹{pnl_val:+.2f}" if pnl_val is not None else "—"
        lots     = (pos.quantity // pos.lot_size) if (pos and pos.lot_size) else (order.quantity or "—")
        t        = order.placed_at.strftime("%m/%d %H:%M") if order.placed_at else "—"
        rows_html += (
            f"<tr>"
            f"<td>{t}</td>"
            f"<td style='font-weight:600'>{order.tradingsymbol}</td>"
            f"<td>{order.transaction_type}</td>"
            f"<td class='tr'>{lots}</td>"
            f"<td class='tr'>{entry_px}</td>"
            f"<td class='tr bd'>{sl}</td>"
            f"<td class='tr ok'>{tgt}</td>"
            f"<td class='tr {pnl_cls(pnl_val)}'>{pnl_str}</td>"
            f"<td>{status_badge(order, ct)}</td>"
            f"</tr>"
        )

    return Response(
        content=_shell("orders",
            filters + summary
            + "<div class='card'><div class='ct'>Orders</div>"
            "<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th class='tr'>Lots</th>"
            "<th class='tr'>Entry</th><th class='tr'>SL</th><th class='tr'>Target</th>"
            "<th class='tr'>P&amp;L</th><th>Status</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
            refresh=True,
        ),
        media_type="text/html",
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/auth/status")
async def auth_status(settings: Settings = Depends(get_current_settings)) -> dict:
    try:
        token_info = get_session_manager().get_token_info()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Session manager not configured: {exc}")
    checked_at = get_last_checked_at()
    return {
        "session_valid": token_info["is_valid"],
        "token_age_hours": token_info["age_hours"],
        "reason": token_info["reason"],
        "dry_run": settings.DRY_RUN,
        "checked_at": checked_at.isoformat() if checked_at is not None else None,
    }
