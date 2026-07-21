from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
    Alert, AppError, ClosedTrade, Gtt, Instrument, Order, PnlSnapshot, Position,
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

def _realised_loss_today(session: Any, paper: bool = False) -> float:
    """Total realised loss (positive ₹) today IST, scoped to paper or live trades."""
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(ClosedTrade.pnl)
        .filter(
            ClosedTrade.closed_at >= today_start,
            ClosedTrade.pnl < 0,
            ClosedTrade.dry_run == paper,
        )
        .all()
    )
    return abs(sum(r[0] for r in rows))


def _realised_pnl_today(session: Any, paper: bool = False) -> float:
    """Net realised P&L (signed ₹) today IST, scoped to paper or live trades."""
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(ClosedTrade.pnl)
        .filter(ClosedTrade.closed_at >= today_start, ClosedTrade.dry_run == paper)
        .all()
    )
    return float(sum(r[0] for r in rows if r[0] is not None))


def _open_position_count(session: Any, paper: bool = False) -> int:
    return (
        session.query(Position)
        .join(Order, Position.order_id == Order.id)
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(ClosedTrade.id == None, Order.dry_run == paper)  # noqa: E711
        .count()
    )


def _trades_today_count(session: Any, paper: bool = False) -> int:
    """Distinct entry signals that produced an order today, scoped by mode.

    Counts distinct alerts (not orders) so a 2-leg straddle is one trade —
    matching the old Alert-based count — while letting paper trades stop
    consuming the live day's trade budget after a mode toggle."""
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        session.query(Order.alert_id)
        .join(Alert, Alert.id == Order.alert_id)
        .filter(
            Alert.action.in_(["BUY", "SELL"]),
            Order.placed_at >= today_start,
            Order.dry_run == paper,
        )
        .distinct()
        .count()
    )


def _fetch_adx_for_underlying(underlying: Any, settings: Any, kite: Any, session: Any, now: datetime) -> float | None:
    """Fetch ADX for `underlying` from Kite historical data.

    Used by RANGE_SELL when the TV alert omits the adx field. Returns None on any failure.
    NSE indices use the spot index instrument; MCX uses the near-month futures contract.
    """
    from app.adx import compute_adx

    period = settings.ADX_PERIOD
    interval = settings.ADX_CANDLE_INTERVAL
    interval_mins = int(interval.replace("minute", "").replace("min", "")) if "min" in interval else 1
    n_candles = period * 3 + 5

    if underlying.segment == "MCX":
        instr = (
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
    else:
        # NSE/BFO indices: spot_source is like "NSE:NIFTY 50"
        parts = underlying.spot_source.split(":", 1)
        exch, ts = (parts[0], parts[1]) if len(parts) == 2 else ("NSE", parts[0])
        instr = (
            session.query(Instrument)
            .filter(Instrument.exchange == exch, Instrument.tradingsymbol == ts)
            .first()
        )

    if instr is None:
        log.warning("RANGE_SELL ADX: no instrument found for %s", underlying.name)
        return None

    # Look back 7 calendar days so weekends/holidays never cause a candle shortage.
    # 7 days ≈ 5 trading days × 375 NSE min = 187 10-min candles, well above n_candles=47.
    from_dt = now - timedelta(days=7)
    try:
        raw = kite.historical_data(instr.instrument_token, from_dt, now, interval)
    except Exception as exc:
        log.warning("RANGE_SELL ADX: historical_data failed for %s: %s", underlying.name, exc)
        return None

    if not raw:
        log.warning("RANGE_SELL ADX: empty candles for %s", underlying.name)
        return None

    adx = compute_adx(raw, period=period)
    log.info(
        "RANGE_SELL ADX(%d) %s on %s = %s",
        period, underlying.name, interval,
        f"{adx:.2f}" if adx is not None else "None",
    )
    return adx


def _consecutive_losses(session: Any, paper: bool = False) -> int:
    rows = (
        session.query(ClosedTrade.exit_reason)
        .filter(ClosedTrade.dry_run == paper)
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
    # Guards evaluate trades of the active mode only, so a paper session
    # exercises the same guard behavior live trading would see — and paper
    # losses never block live entries after a mode toggle (or vice versa).
    paper = _dry_run(settings)
    if state.is_emergency_stop():
        return False, "EMERGENCY STOP is active"
    loss = _realised_loss_today(session, paper=paper)
    effective_loss_cap = state.get_max_daily_loss(settings.MAX_DAILY_LOSS_ABS)
    if loss >= effective_loss_cap:
        return False, f"daily loss ₹{loss:.2f} >= limit ₹{effective_loss_cap:.2f}"
    trades = _trades_today_count(session, paper=paper)
    eff_max_trades = state.get_max_trades_per_day(settings.MAX_TRADES_PER_DAY)
    if trades >= eff_max_trades:
        return False, f"trades today {trades} >= MAX_TRADES_PER_DAY {eff_max_trades}"
    positions = _open_position_count(session, paper=paper)
    eff_max_positions = state.get_max_open_positions(settings.MAX_OPEN_POSITIONS)
    if positions >= eff_max_positions:
        return False, f"open positions {positions} >= MAX_OPEN_POSITIONS {eff_max_positions}"
    losses = _consecutive_losses(session, paper=paper)
    eff_consec_limit = state.get_consecutive_losses_limit(settings.CONSECUTIVE_LOSSES_LIMIT)
    if losses >= eff_consec_limit:
        return False, f"consecutive losses {losses} >= CONSECUTIVE_LOSSES_LIMIT {eff_consec_limit}"
    return True, ""


def _round_to_tick(price: float, tick: float) -> float:
    """Round price to nearest valid tick boundary (handles MCX 0.05/0.10 ticks)."""
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 2)


def _scorecard_group(strategy_id: str | None) -> str:
    """Collapse per-trade-unique strategy_ids into stable scorecard buckets.

    TradingView alert templates commonly stamp {{timenow}}-style unique ids
    (\"1780293014253\", \"2026-05-12T16:02:13Z\", \"BANKNIFTY-1781511000000\"),
    and voice orders carry per-order uuid suffixes — without collapsing these,
    every trade would be its own scorecard row."""
    sid = strategy_id or "manual"
    if re.fullmatch(r"voice_[0-9a-f]{6,}", sid):
        return "voice"
    if re.fullmatch(r"straddle_[0-9a-f]{6,}", sid):
        return "voice_straddle"
    if (
        re.fullmatch(r"\d{9,}", sid)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-9:.Z+-]+", sid)
        or re.fullmatch(r"[A-Z0-9]+-\d{9,}", sid)
    ):
        return "tv_webhook"
    return sid


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
        # Capture before commit expires the ORM object
        _straddle_id: str | None = entry_order.straddle_id
        _entry_kite_order_id: str = entry_order.kite_order_id
        session.commit()
        log.info(
            "_on_gtt_filled: %s pnl=%.2f order=%s",
            event.tradingsymbol, float(pnl), _entry_kite_order_id,
        )

    # Stop trailing now that the position is closed
    if _trailing_manager is not None and _instr_token is not None:
        _trailing_manager.unregister(_instr_token)
        if _watcher is not None:
            _watcher.unsubscribe([_instr_token])

    # ── Straddle paired-leg exit ──────────────────────────────────────────────
    # When one straddle leg is stopped out, immediately close the other leg too.
    # The backtest strategy exits BOTH legs the moment either leg hits 1.5× entry.
    if _straddle_id and _SessionFactory is not None:
        try:
            kite = get_session_manager().get_kite()
        except Exception as _exc:
            log.error("_on_gtt_filled: cannot get kite for paired-leg exit: %s", _exc)
            kite = None

        if kite is not None:
            with _SessionFactory() as session2:
                sibling_orders = (
                    session2.query(Order)
                    .filter(
                        Order.straddle_id == _straddle_id,
                        Order.kite_order_id != _entry_kite_order_id,
                    )
                    .all()
                )
                for sib_order in sibling_orders:
                    sib_pos = (
                        session2.query(Position)
                        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
                        .filter(
                            Position.order_id == sib_order.id,
                            ClosedTrade.id == None,  # noqa: E711
                        )
                        .first()
                    )
                    if sib_pos is None:
                        continue  # already closed

                    sib_instr = (
                        session2.query(Instrument)
                        .filter(
                            Instrument.tradingsymbol == sib_pos.tradingsymbol,
                            Instrument.exchange == sib_pos.exchange,
                        )
                        .first()
                    )
                    if sib_instr is None:
                        log.error(
                            "_on_gtt_filled: paired-leg instrument not found for %s",
                            sib_pos.tradingsymbol,
                        )
                        continue

                    # Cancel active GTT on sibling
                    sib_gtt = (
                        session2.query(Gtt)
                        .filter(Gtt.order_id == sib_order.id, Gtt.status == "ACTIVE")
                        .first()
                    )
                    if sib_gtt and sib_gtt.kite_gtt_id:
                        try:
                            cancel_gtt(kite, sib_gtt.kite_gtt_id)
                        except Exception as _exc:
                            log.warning(
                                "_on_gtt_filled: cancel GTT %d for paired leg %s failed: %s",
                                sib_gtt.kite_gtt_id, sib_pos.tradingsymbol, _exc,
                            )
                        sib_gtt.status = "CANCELLED"
                        sib_gtt.updated_at = datetime.now(IST)

                    # Unregister trailing SL for sibling
                    if _trailing_manager is not None and sib_instr:
                        try:
                            _trailing_manager.unregister(sib_instr.instrument_token)
                        except Exception:
                            pass

                    # Fetch LTP for a limit exit; fall back to MARKET
                    limit_px: float | None = None
                    try:
                        q_key = f"{sib_pos.exchange}:{sib_pos.tradingsymbol}"
                        q = kite.quote([q_key])
                        ltp = float(q.get(q_key, {}).get("last_price", 0.0))
                        if ltp > 0:
                            from app.symbol_mapper import round_to_tick
                            tick = float(sib_instr.tick_size) if sib_instr.tick_size else 0.05
                            limit_px = round_to_tick(ltp * 1.005, tick)
                    except Exception as _exc:
                        log.warning(
                            "_on_gtt_filled: LTP fetch failed for paired leg %s — using MARKET: %s",
                            sib_pos.tradingsymbol, _exc,
                        )

                    try:
                        sq_id = square_off(
                            kite, sib_instr, sib_pos.quantity,
                            product=sib_order.product or "NRML",
                            entry_side=sib_order.transaction_type or "SELL",
                            limit_price=limit_px,
                        )
                        now2 = datetime.now(IST)
                        from app.storage import booked_partial_pnl, trade_meta_for_order
                        _sib_sid, _sib_dry = trade_meta_for_order(session2, sib_order)
                        ct = ClosedTrade(
                            position_id=sib_pos.id,
                            exchange=sib_pos.exchange,
                            tradingsymbol=sib_pos.tradingsymbol,
                            entry_premium=sib_pos.entry_premium,
                            exit_premium=0.0,
                            pnl=booked_partial_pnl(sib_pos),
                            exit_reason="straddle_paired_sl_exit",
                            opened_at=sib_pos.opened_at,
                            closed_at=now2,
                            strategy_id=_sib_sid,
                            dry_run=_sib_dry,
                        )
                        session2.add(ct)
                        log.info(
                            "_on_gtt_filled: paired-leg %s closed sq_order=%s (straddle_id=%s)",
                            sib_pos.tradingsymbol, sq_id, _straddle_id[:8],
                        )
                    except Exception as _exc:
                        log.error(
                            "_on_gtt_filled: paired-leg squareoff failed for %s: %s",
                            sib_pos.tradingsymbol, _exc,
                            exc_info=True,
                        )
                session2.commit()


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

        # Defined-risk entry: wings go on BEFORE the shorts, so the position is
        # never naked. straddle_id is minted here because the wings are tagged
        # with it and recorded against it once the shorts fill.
        straddle_id = str(_uuid.uuid4())
        wing_plan = None
        if state.is_entry_wings_enabled(settings.STRADDLE_ENTRY_WINGS_ENABLED):
            from app.entry_wings import attach_entry_wings
            try:
                _wing_kite = get_session_manager().get_kite()
            except Exception as _wexc:
                _wing_kite = None
                log.warning("_process_straddle %d: no Kite session for wings (%s)", alert_id, _wexc)
            wing_plan, wing_block = attach_entry_wings(
                session, settings,
                underlying=underlying, exchange=_straddle_exchange,
                ce_short_symbol=ce_sym, pe_short_symbol=pe_sym,
                quantity=total_qty, product=product, kite=_wing_kite, dry=dry,
            )
            if wing_block is not None:
                log.error("_process_straddle %d aborted — wings required but %s",
                          alert_id, wing_block)
                try:
                    from app.commodity_agents.notify import send_telegram
                    send_telegram(settings, (
                        f"\U0001f6d1 <b>{underlying} straddle skipped</b> — "
                        f"defined-risk wings unavailable: {wing_block}"
                    ))
                except Exception:
                    pass
                alert.processed = True
                session.commit()
                return

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
                if wing_plan is not None:
                    from app.entry_wings import reverse_entry_wings
                    reverse_entry_wings(wing_plan, kite_client, product)
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
                if wing_plan is not None:
                    from app.entry_wings import reverse_entry_wings
                    reverse_entry_wings(wing_plan, kite_client, product)
                alert.processed = True
                session.commit()
                return

            if not ce_kite_id and not pe_kite_id and wing_plan is not None:
                log.error("_process_straddle %d: both short legs failed — reversing wings", alert_id)
                from app.entry_wings import reverse_entry_wings
                reverse_entry_wings(wing_plan, kite_client, product)
                wing_plan = None
                alert.processed = True
                session.commit()
                return
        else:
            # Paper straddle: simulate both leg fills at LTP so Position/Gtt
            # rows exist and the paper monitor can manage exits. Falls back to
            # stub Order rows (no fill) when quotes are unavailable.
            _paper_ltps: dict[str, float] = {}
            try:
                from app.paper_trading import new_paper_order_id
                _kite_q = get_session_manager().get_kite()
                _q_keys = [f"{_straddle_exchange}:{ce_sym}", f"{_straddle_exchange}:{pe_sym}"]
                _quotes = _kite_q.ltp(_q_keys)
                _paper_ltps["CE"] = float(_quotes[_q_keys[0]]["last_price"])
                _paper_ltps["PE"] = float(_quotes[_q_keys[1]]["last_price"])
                ce_kite_id = new_paper_order_id()
                pe_kite_id = new_paper_order_id()
                log.info(
                    "_process_straddle %d DRY_RUN — simulated SELL %s @ %.2f + SELL %s @ %.2f qty=%d product=%s",
                    alert_id, ce_sym, _paper_ltps["CE"], pe_sym, _paper_ltps["PE"], total_qty, product,
                )
            except Exception as _exc:
                _paper_ltps.clear()
                log.warning(
                    "_process_straddle %d DRY_RUN — quotes unavailable (%s); recording stub: "
                    "would SELL %s + SELL %s qty=%d product=%s",
                    alert_id, _exc, ce_sym, pe_sym, total_qty, product,
                )

        # straddle_id (minted above, before the wings) links the Order rows
        for kite_oid, instr_type in [(ce_kite_id, "CE"), (pe_kite_id, "PE")]:
            if kite_oid:
                _pending_order_meta[kite_oid] = {
                    "instrument_type": instr_type,
                    "entry_side": "SELL",
                    "underlying": underlying,
                    "straddle_id": straddle_id,
                    "dry_run": dry,
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

        # Wings are recorded only now: the shorts are on, so the HedgeAction
        # that marks this straddle as already-hedged is accurate.
        if wing_plan is not None:
            from app.entry_wings import record_entry_wings
            record_entry_wings(
                session, settings, wing_plan, wing_plan.get("orders", {}),
                straddle_key=straddle_id, now=now2, dry=dry, product=product,
            )

        alert.processed = True
        session.commit()

        if dry:
            # Simulated fills — same handler as live: Position + DRY_RUN Gtt rows.
            for kite_oid, instr_type in [(ce_kite_id, "CE"), (pe_kite_id, "PE")]:
                if kite_oid and instr_type in _paper_ltps:
                    _on_entry_filled(EntryFilledEvent(
                        kite_order_id=kite_oid,
                        fill_price=_paper_ltps[instr_type],
                        fill_qty=total_qty,
                    ))
        elif _watcher is not None:
            for kite_oid in [ce_kite_id, pe_kite_id]:
                if kite_oid:
                    _watcher.watch_order(kite_oid, kite_fetcher=get_session_manager().get_kite)
        else:
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

from app.commodity_agents.routes import router as _commodity_agents_router  # noqa: E402
app.include_router(_commodity_agents_router)


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

        if underlying.name in settings.WEBHOOK_BLOCKED_UNDERLYINGS:
            log.warning(
                "Alert %d: %s is in WEBHOOK_BLOCKED_UNDERLYINGS — skipping",
                alert_id, underlying.name,
            )
            alert.processed = True
            session.commit()
            return

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

        # Risk gates run in BOTH modes, scoped to that mode's trades — a paper
        # session exercises the same halt behavior live trading would see.
        try:
            risk.check_risk_gates(
                session, now,
                state.get_consecutive_losses_limit(settings.CONSECUTIVE_LOSSES_LIMIT),
                dry_run=_dry_run(settings),
            )
        except risk.RiskHaltError as exc:
            log.warning("Alert %d: risk halt — %s", alert_id, exc.reason)
            alert.processed = True
            session.commit()
            return

        # ── Daily profit target circuit breaker ───────────────────────────────
        _profit_target = state.get_daily_profit_target(settings.DAILY_PROFIT_TARGET)
        if _profit_target > 0:
            _today_pnl = risk.daily_realized_pnl(session, now, dry_run=_dry_run(settings))
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
                _eq_paper = _dry_run(settings)
                kite_order_id = None
                if not _eq_paper:
                    kite_order_id = place_entry(kite_client, eq_instrument, "BUY", qty, _eq_ot, "CNC", price=_eq_lp)
                else:
                    from app.paper_trading import new_paper_order_id
                    kite_order_id = new_paper_order_id()
                    log.info(
                        "Alert %d: DRY_RUN — simulated BUY %d shares %s %s CNC at ltp=%.2f (%s)",
                        alert_id, qty, eq_instrument.tradingsymbol, _eq_ot, ltp, kite_order_id,
                    )
                if kite_order_id:
                    _pending_order_meta[kite_order_id] = {
                        "instrument_type": "EQ",
                        "underlying": underlying.name,
                        "dry_run": _eq_paper,
                    }

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
                    status="PENDING" if not _eq_paper else "DRY_RUN",
                    placed_at=now,
                    updated_at=now,
                    dry_run=_eq_paper,
                )
                session.add(order)
                session.flush()

                alert.processed = True
                session.commit()
                # Register AFTER commit so _on_entry_filled can find the Order row.
                if kite_order_id and _eq_paper:
                    _on_entry_filled(EntryFilledEvent(
                        kite_order_id=kite_order_id,
                        fill_price=float(_eq_lp) if _eq_lp else ltp,
                        fill_qty=qty,
                    ))
                elif kite_order_id:
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
            # Scope to the active mode: a paper EXIT must never DB-close a live
            # position (its GTT would stay armed at Kite while we think it's flat).
            position = (
                session.query(Position)
                .join(Order, Position.order_id == Order.id)
                .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
                .filter(
                    Position.tradingsymbol == ts,
                    ClosedTrade.id == None,  # noqa: E711
                    Order.dry_run == _dry_run(settings),
                )
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

            _exit_premium = 0.0
            _exit_pnl = 0.0
            if not _dry_run(settings):
                kite_client = get_session_manager().get_kite()
                if gtt and gtt.kite_gtt_id:
                    cancel_gtt(kite_client, gtt.kite_gtt_id)
                if gtt:
                    gtt.status = "CANCELLED"
                if instrument:
                    square_off(kite_client, instrument, position.quantity, exit_product)
            else:
                # Paper exit: mark to LTP so the simulated trade has a real PnL.
                log.info("Alert %d: DRY_RUN — EXIT %s qty=%d product=%s at LTP", alert_id, ts, position.quantity, exit_product)
                if gtt:
                    gtt.status = "CANCELLED"
                try:
                    from app.paper_trading import paper_pnl
                    _q_key = f"{position.exchange}:{ts}"
                    _kite_q = get_session_manager().get_kite()
                    _exit_premium = float(_kite_q.ltp([_q_key])[_q_key]["last_price"])
                    _exit_pnl = paper_pnl(
                        entry_order.transaction_type if entry_order else "BUY",
                        position.entry_premium, _exit_premium, position.quantity,
                        position.exchange, position.underlying or ts, settings,
                    )
                except Exception as _exc:
                    log.warning("Alert %d: paper EXIT LTP unavailable for %s (%s) — recording pnl=0", alert_id, ts, _exc)

            from app.storage import booked_partial_pnl, trade_meta_for_order
            _exit_sid, _exit_dry = trade_meta_for_order(session, entry_order)
            ct = ClosedTrade(
                position_id=position.id,
                exchange=position.exchange,
                tradingsymbol=position.tradingsymbol,
                entry_premium=position.entry_premium,
                exit_premium=_exit_premium,
                pnl=_exit_pnl + booked_partial_pnl(position),
                exit_reason="MANUAL_EXIT",
                opened_at=position.opened_at,
                closed_at=now,
                strategy_id=_exit_sid,
                dry_run=_exit_dry,
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
        else:  # RANGE_SELL: sell same-direction when ranging; fall back to SELL_OPTIONS when trending; skip if ADX unavailable
            adx_threshold = state.get_adx_threshold(settings.ADX_THRESHOLD)
            adx_value = alert_data.adx

            if adx_value is None:
                # TV didn't send ADX — fetch it from Kite on ADX_CANDLE_INTERVAL
                _rs_kite = get_session_manager().get_kite()
                adx_value = _fetch_adx_for_underlying(underlying, settings, _rs_kite, session, now)

            if adx_value is None:
                # ADX still unavailable (e.g. insufficient candles at market open) → skip
                log.warning(
                    "Alert %d: RANGE_SELL ADX unavailable — skipping trade",
                    alert_id,
                )
                alert.processed = True
                session.commit()
                return
            elif adx_value < adx_threshold:
                # Ranging market: sell same-direction (contrarian)
                log.info(
                    "Alert %d: RANGE_SELL ADX=%.1f < %.1f (ranging) — sell %s",
                    alert_id, adx_value, adx_threshold,
                    "CE" if alert_data.action == "BUY" else "PE",
                )
                flag = "CE" if alert_data.action == "BUY" else "PE"
                entry_side = "SELL"
            else:
                # Trending (ADX >= threshold) → fall back to SELL_OPTIONS
                log.info(
                    "Alert %d: RANGE_SELL ADX=%.1f >= %.1f (trending) — falling back to SELL_OPTIONS",
                    alert_id, adx_value, adx_threshold,
                )
                flag = "PE" if alert_data.action == "BUY" else "CE"
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

        # Paper mode runs the FULL pipeline — expiry, strike selection, sizing,
        # then a simulated fill at LTP — so a paper session produces the same
        # decisions and a real track record. Falls back to a stub Order only
        # when there's no Kite session for market data.
        _paper = _dry_run(settings)
        if _paper:
            _paper_session_ok = False
            try:
                _paper_session_ok = (
                    bool(get_session_manager().get_token_info().get("is_valid"))
                    and not state.SESSION_INVALID
                )
            except Exception:
                pass
            if _paper_session_ok:
                log.info(
                    "[%s] Alert %d: DRY_RUN — simulating %s %s option for %s "
                    "(segment=%s) via full pipeline",
                    trade_mode, alert_id, entry_side, flag, underlying.name, underlying.segment,
                )
            else:
                log.info(
                    "[%s] Alert %d: DRY_RUN — no Kite session for simulation; "
                    "recording stub: would %s %s option for %s (segment=%s)",
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
        daily_remaining_opt = risk.daily_loss_remaining(session, now, dry_run=_dry_run(settings))
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
        if _paper:
            from app.paper_trading import new_paper_order_id
            kite_order_id = new_paper_order_id()
            log.info(
                "[%s] Alert %d: DRY_RUN — simulated %s %d lot(s) %s @ %.2f (%s)",
                trade_mode, alert_id, entry_side, lots_count,
                selection.instrument.tradingsymbol,
                float(_opt_lp) if _opt_lp else option_ltp, kite_order_id,
            )
        else:
            kite_order_id = place_entry(
                kite_client, selection.instrument, entry_side, qty, _opt_ot, product, price=_opt_lp
            )
        if kite_order_id:
            _pending_order_meta[kite_order_id] = {
                "instrument_type": flag,
                "underlying": underlying.name,
                "entry_side": entry_side,
                "dry_run": _paper,
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
            status="DRY_RUN" if _paper else "PENDING",
            placed_at=now,
            updated_at=now,
            dry_run=_paper,
        )
        session.add(order)
        session.flush()

        alert.processed = True
        session.commit()
        # Register AFTER commit so _on_entry_filled can find the Order row.
        # MARKET orders fill in milliseconds; committing first closes the race.
        if kite_order_id:
            if _paper:
                # Simulated fill at LTP (or the requested limit) — same handler
                # as a live fill: Position + DRY_RUN Gtt row, no Kite calls.
                _on_entry_filled(EntryFilledEvent(
                    kite_order_id=kite_order_id,
                    fill_price=float(_opt_lp) if _opt_lp else option_ltp,
                    fill_qty=qty,
                ))
            elif _watcher is not None:
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
    # ── Design tokens — light default, dark via prefers-color-scheme ─────────
    ":root{"
    "color-scheme:light dark;"
    "--bg:#F5F5F7;--surface:#FFFFFF;--surface2:#FAFAFC;"
    "--ink:#1D1D1F;--ink2:#6E6E73;--ink3:#A6A6AB;"
    "--line:rgba(0,0,0,.09);--line-soft:rgba(0,0,0,.05);"
    "--accent:#0071E3;--on-accent:#FFFFFF;"
    "--gain:#1F8A3B;--loss:#D70015;--warn:#B25000;"
    "--gain-b:#34C759;--loss-b:#FF3B30;--warn-b:#FF9500;"
    "--gain-soft:rgba(52,199,89,.13);--loss-soft:rgba(255,59,48,.10);"
    "--warn-soft:rgba(255,159,10,.16);--accent-soft:rgba(0,113,227,.10);"
    "--glass:rgba(245,245,247,.80);"
    "--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.05)"
    "}"
    "@media (prefers-color-scheme:dark){:root{"
    "--bg:#0A0A0C;--surface:#16161A;--surface2:#1D1D22;"
    "--ink:#F5F5F7;--ink2:#98989D;--ink3:#636368;"
    "--line:rgba(255,255,255,.10);--line-soft:rgba(255,255,255,.06);"
    "--accent:#0A84FF;--on-accent:#FFFFFF;"
    "--gain:#30D158;--loss:#FF453A;--warn:#FF9F0A;"
    "--gain-b:#30D158;--loss-b:#FF453A;--warn-b:#FF9F0A;"
    "--gain-soft:rgba(48,209,88,.15);--loss-soft:rgba(255,69,58,.13);"
    "--warn-soft:rgba(255,159,10,.15);--accent-soft:rgba(10,132,255,.14);"
    "--glass:rgba(10,10,12,.72);"
    "--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.45)"
    "}}"

    # ── Reset / base ─────────────────────────────────────────────────────────
    "*{box-sizing:border-box;margin:0;padding:0}"
    "body{"
    "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','SF Pro Text',"
    "'Segoe UI Variable Display','Segoe UI',Roboto,Arial,sans-serif;"
    "background:var(--bg);color:var(--ink);min-height:100vh;font-size:15px;"
    "-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale"
    "}"
    ":focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}"

    # ── Topbar — single frosted sticky bar: brand + nav + meta ───────────────
    ".topbar{"
    "position:sticky;top:0;z-index:100;"
    "background:var(--glass);"
    "backdrop-filter:saturate(180%) blur(20px);"
    "-webkit-backdrop-filter:saturate(180%) blur(20px);"
    "border-bottom:1px solid var(--line-soft)"
    "}"
    ".tb-in{"
    "max-width:1200px;margin:0 auto;display:flex;align-items:center;gap:10px;"
    "min-height:50px;padding:6px 20px;flex-wrap:wrap"
    "}"
    ".wordmark{font-size:15px;font-weight:700;letter-spacing:-.02em;white-space:nowrap}"
    ".wordmark i{display:inline-block;width:8px;height:8px;border-radius:50%;"
    "background:var(--accent);margin-right:7px;vertical-align:baseline;font-style:normal}"
    ".tb-nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;-ms-overflow-style:none}"
    ".tb-nav::-webkit-scrollbar{display:none}"
    ".tb-nav a{"
    "color:var(--ink2);text-decoration:none;padding:6px 11px;border-radius:8px;"
    "font-size:12.5px;font-weight:600;white-space:nowrap;letter-spacing:-.01em;"
    "transition:color .15s,background .15s"
    "}"
    ".tb-nav a:hover{color:var(--ink);background:var(--line-soft)}"
    ".tb-nav a.on{color:var(--accent);background:var(--accent-soft)}"
    ".tb-lut{margin-left:auto;font-size:11px;color:var(--ink3);white-space:nowrap;"
    "font-variant-numeric:tabular-nums}"
    ".tb-out{color:var(--loss);font-size:12px;font-weight:600;text-decoration:none;"
    "white-space:nowrap;padding:6px 4px 6px 10px}"

    # ── Layout ───────────────────────────────────────────────────────────────
    # Bottom padding reserves space for the fixed .tabbar (~50px tall, plus
    # the home-indicator safe area added below on touch devices) so page
    # content never sits underneath it.
    ".wrap{padding:20px 20px 84px;margin:0 auto}"
    ".wrap-sm{max-width:640px}.wrap-lg{max-width:1024px}"

    # ── Cards ────────────────────────────────────────────────────────────────
    ".card{"
    "background:var(--surface);border:1px solid var(--line-soft);"
    "border-radius:16px;margin-bottom:20px;overflow:hidden;box-shadow:var(--shadow)"
    "}"
    "@media (prefers-reduced-motion:no-preference){"
    ".card{animation:crise .4s cubic-bezier(.2,.7,.3,1) both}"
    "@keyframes crise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}"
    "}"

    # ── Card section header ───────────────────────────────────────────────────
    ".ct{"
    "font-size:11.5px;font-weight:650;text-transform:uppercase;letter-spacing:.08em;"
    "color:var(--ink2);padding:14px 18px 10px;"
    "border-bottom:1px solid var(--line-soft);"
    "display:flex;align-items:center;gap:6px"
    "}"

    # ── Status pills ──────────────────────────────────────────────────────────
    ".pill{"
    "display:inline-flex;align-items:center;"
    "padding:3px 10px;border-radius:100px;"
    "font-size:11.5px;font-weight:600;letter-spacing:.01em;white-space:nowrap"
    "}"
    ".pg{background:var(--gain-soft);color:var(--gain)}"
    ".pr{background:var(--loss-soft);color:var(--loss)}"
    ".pa{background:var(--warn-soft);color:var(--warn)}"
    ".pb{background:var(--accent-soft);color:var(--accent)}"
    ".pm{background:var(--line-soft);color:var(--ink2)}"
    ".pp{background:rgba(88,86,214,.12);color:#7D7AFF}"
    "@media (prefers-color-scheme:light){.pp{color:#3634A3}}"

    # ── Progress bar rows ─────────────────────────────────────────────────────
    ".mr{display:flex;align-items:center;gap:10px;padding:11px 18px;"
    "border-bottom:1px solid var(--line-soft)}"
    ".mr:last-of-type{border-bottom:none}"
    ".ml{font-size:12.5px;color:var(--ink2);width:120px;flex-shrink:0;font-weight:500}"
    ".mw{flex:1;height:6px;background:var(--line-soft);border-radius:3px;overflow:hidden}"
    ".mb{height:100%;border-radius:3px;transition:width .5s cubic-bezier(.25,.46,.45,.94)}"
    ".mv{font-size:12.5px;font-weight:650;min-width:95px;text-align:right;"
    "font-variant-numeric:tabular-nums}"

    # ── Value colours ─────────────────────────────────────────────────────────
    ".ok{color:var(--gain)}.wn{color:var(--warn)}.bd{color:var(--loss)}"
    ".bok{background:var(--gain-b)}.bwn{background:var(--warn-b)}.bbd{background:var(--loss-b)}"

    # ── Settings rows ─────────────────────────────────────────────────────────
    ".mdr{"
    "display:flex;align-items:center;justify-content:space-between;gap:10px;"
    "padding:12px 18px;border-bottom:1px solid var(--line-soft)"
    "}"
    ".mdr:last-of-type{border-bottom:none}"
    ".mdl{font-size:13.5px;color:var(--ink);font-weight:500;display:flex;"
    "align-items:center;gap:8px;flex-wrap:wrap;min-width:0}"

    # ── Buttons ───────────────────────────────────────────────────────────────
    ".btn{"
    "padding:8px 15px;border:1px solid transparent;border-radius:10px;"
    "font-size:12.5px;font-weight:600;cursor:pointer;"
    "transition:opacity .15s,transform .1s,background .15s;color:var(--on-accent);"
    "font-family:inherit;letter-spacing:-.01em;"
    "display:inline-flex;align-items:center;justify-content:center;white-space:nowrap"
    "}"
    ".btn:active{transform:scale(.97)}"
    ".btn:hover{opacity:.88}"
    ".bn{background:var(--surface2);border-color:var(--line);color:var(--ink)}"
    ".bg2{background:var(--gain-b);color:#fff}"
    ".br2{background:transparent;border-color:rgba(255,69,58,.5);color:var(--loss)}"
    ".br2:hover{background:var(--loss-soft);opacity:1}"
    ".ba{background:transparent;border-color:rgba(255,159,10,.55);color:var(--warn)}"
    ".ba:hover{background:var(--warn-soft);opacity:1}"
    ".bp{background:var(--accent)}"
    ".bm{background:var(--line-soft);color:var(--ink2)}"
    ".bfull{"
    "display:block;width:calc(100% - 36px);margin:12px 18px 16px;"
    "padding:13px;font-size:14px;text-align:center;border-radius:12px"
    "}"

    # ── Emergency stop banner ─────────────────────────────────────────────────
    ".sbanner{"
    "background:var(--loss-b);"
    "color:#fff;padding:14px 18px;border-radius:14px;"
    "font-weight:700;text-align:center;"
    "margin-bottom:16px;font-size:14px;letter-spacing:.01em;"
    "box-shadow:0 4px 16px rgba(255,59,48,.30)"
    "}"

    # ── Risk param rows ───────────────────────────────────────────────────────
    ".pr2{display:flex;align-items:center;gap:8px;padding:10px 18px;"
    "border-bottom:1px solid var(--line-soft)}"
    ".pr2:last-of-type{border-bottom:none}"
    ".pl{flex:1;font-size:13px;color:var(--ink);font-weight:500}"
    ".pi{"
    "width:96px;padding:9px 10px;"
    "border:1px solid var(--line);border-radius:8px;"
    # 16px minimum — iOS Safari auto-zooms the page on focus of any input
    # below that, which is jarring on a page meant to live on an iPhone.
    "font-size:16px;text-align:right;background:var(--surface2);color:var(--ink);"
    "font-family:inherit;font-variant-numeric:tabular-nums;"
    "transition:border-color .2s,box-shadow .2s"
    "}"
    ".pi:focus{outline:none;border-color:var(--accent);background:var(--surface);"
    "box-shadow:0 0 0 3px var(--accent-soft)}"
    ".pu{font-size:11.5px;color:var(--ink3);width:26px;text-align:left}"

    # ── Override dot ──────────────────────────────────────────────────────────
    ".od{display:inline-block;width:6px;height:6px;border-radius:50%;"
    "background:var(--warn-b);margin-left:4px;vertical-align:middle}"

    # ── Tables ────────────────────────────────────────────────────────────────
    "table{width:100%;border-collapse:collapse}"
    "th{"
    "font-size:10.5px;font-weight:650;text-transform:uppercase;letter-spacing:.07em;"
    "color:var(--ink3);padding:10px 14px;"
    "border-bottom:1px solid var(--line);text-align:left"
    "}"
    "td{padding:10px 14px;font-size:13px;border-bottom:1px solid var(--line-soft);"
    "color:var(--ink);font-variant-numeric:tabular-nums}"
    "tr:last-child td{border-bottom:none}"
    "tbody tr:hover td{background:var(--surface2)}"
    ".tc{text-align:center}.tr{text-align:right}"

    # ── Annunciator strip (system-state pills) ────────────────────────────────
    ".strip{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}"
    ".strip .pill{gap:5px}"
    ".sdot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}"
    "#pill-kite.pr{background:var(--loss-b);color:#fff}"

    # ── Today hero (P&L) ──────────────────────────────────────────────────────
    ".hero-pnl{font-size:2.7em;font-weight:650;letter-spacing:-.03em;line-height:1.1;"
    "font-variant-numeric:tabular-nums;padding:14px 18px 2px}"
    ".hero-sub{display:flex;gap:6px 16px;flex-wrap:wrap;padding:4px 18px 14px;"
    "font-size:12.5px;color:var(--ink2)}"
    ".hero-sub b{font-weight:600;font-variant-numeric:tabular-nums;color:var(--ink)}"
    ".hero-sub b.ok{color:var(--gain)}.hero-sub b.bd{color:var(--loss)}"

    # ── Schedule rail ─────────────────────────────────────────────────────────
    ".rail{list-style:none}"
    ".rail li{display:flex;gap:10px;align-items:baseline;padding:8px 18px;"
    "font-size:12.5px;border-bottom:1px solid var(--line-soft)}"
    ".rail li:last-child{border-bottom:none}"
    ".rail .rt{color:var(--ink3);width:46px;flex-shrink:0;font-weight:600;"
    "font-variant-numeric:tabular-nums}"
    ".rail .re{flex:1;color:var(--ink);font-weight:500}"
    ".rail .rs{font-size:11.5px;color:var(--ink3);white-space:nowrap}"
    ".r-done .re,.r-done .rt{color:var(--ink3)}"
    ".r-done .rs{color:var(--gain)}"
    ".r-next{background:var(--accent-soft)}"
    ".r-next .rt,.r-next .re,.r-next .rs{color:var(--accent)}"
    ".r-off .re,.r-off .rt{color:var(--ink3);opacity:.6}"

    # ── Config drawer ─────────────────────────────────────────────────────────
    "details.cfgd summary{list-style:none;cursor:pointer}"
    "details.cfgd summary::-webkit-details-marker{display:none}"
    "details.cfgd summary .ct::after{content:'\\25B8';margin-left:auto;color:var(--ink3);"
    "transition:transform .2s}"
    "details.cfgd[open] summary .ct::after{transform:rotate(90deg)}"
    ".cfg-sum{padding:10px 18px 12px;font-size:12.5px;color:var(--ink2);line-height:1.6}"
    ".cfg-sum b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}"
    "details.cfgd[open] .cfg-sum{display:none}"

    # ── Activity feed ─────────────────────────────────────────────────────────
    ".fchips{display:flex;gap:6px;flex-wrap:wrap;padding:10px 18px 4px}"
    ".fchip{font-size:11.5px;font-weight:600;border-radius:100px;padding:4px 12px;"
    "cursor:pointer;background:var(--line-soft);color:var(--ink2);border:none;"
    "font-family:inherit}"
    ".fchip.on{background:var(--accent);color:var(--on-accent)}"
    ".feed{list-style:none;padding:6px 0 8px}"
    ".feed li{display:flex;gap:9px;align-items:baseline;padding:7px 18px;"
    "font-size:12.5px;border-bottom:1px solid var(--line-soft)}"
    ".feed li:last-child{border-bottom:none}"
    ".feed .ft{color:var(--ink3);width:74px;flex-shrink:0;font-variant-numeric:tabular-nums}"
    ".ftag{flex-shrink:0;font-size:10px;font-weight:700;letter-spacing:.04em;"
    "border-radius:5px;padding:1px 7px;width:48px;text-align:center}"
    ".ft-alert{background:var(--warn-soft);color:var(--warn)}"
    ".ft-order{background:var(--accent-soft);color:var(--accent)}"
    ".ft-gtt{background:rgba(88,86,214,.12);color:#7D7AFF}"
    "@media (prefers-color-scheme:light){.ft-gtt{color:#3634A3}}"
    ".ft-exit{background:var(--gain-soft);color:var(--gain)}"
    ".ft-err{background:var(--loss-soft);color:var(--loss)}"
    ".feed .fe{flex:1;color:var(--ink);overflow-wrap:anywhere}"
    ".fdim{color:var(--ink3)}"

    # ── Commodity intelligence cards ──────────────────────────────────────────
    ".cagrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));"
    "gap:10px;padding:12px 18px 16px}"
    ".cacard{background:var(--surface2);border:1px solid var(--line-soft);"
    "border-radius:12px;padding:11px 12px}"
    ".cacard .cah{display:flex;justify-content:space-between;align-items:center;"
    "margin-bottom:6px;gap:6px}"
    ".cacard .cah b{font-size:13px}"
    ".cakv{display:grid;grid-template-columns:1fr auto;gap:2px 10px;font-size:11.5px;"
    "color:var(--ink3)}"
    ".cakv span:nth-child(even){color:var(--ink);text-align:right;"
    "font-variant-numeric:tabular-nums;font-weight:600}"
    ".cabtns{display:flex;gap:6px;margin-top:9px;flex-wrap:wrap}"
    ".cabtns .btn{padding:5px 11px;font-size:11.5px}"

    # ── Performance ───────────────────────────────────────────────────────────
    ".tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));"
    "gap:14px 18px;padding:14px 18px 8px}"
    ".tile{padding:0}"
    ".tile .tv2{font-size:1.45em;font-weight:650;letter-spacing:-.02em;"
    "font-variant-numeric:tabular-nums}"
    ".tile .tl2{font-size:10.5px;color:var(--ink3);text-transform:uppercase;"
    "letter-spacing:.06em;margin-top:2px;font-weight:600}"
    ".hmwrap{padding:2px 18px 16px}"
    ".hmlbl{font-size:10.5px;color:var(--ink3);font-weight:600;text-transform:uppercase;"
    "letter-spacing:.06em;margin:8px 0 6px}"
    ".hm{display:grid;grid-template-columns:repeat(6,26px);gap:4px}"
    ".hm div{width:26px;height:26px;border-radius:6px;background:var(--line-soft)}"

    # ── Strategy scorecard ───────────────────────────────────────────────────
    ".sct-wrap{padding:6px 18px 14px;overflow-x:auto}"
    ".sct{width:100%;border-collapse:collapse;font-size:12.5px}"
    ".sct th{text-align:left;font-size:10px;color:var(--ink3);text-transform:uppercase;"
    "letter-spacing:.05em;font-weight:650;padding:6px 8px;"
    "border-bottom:1px solid var(--line)}"
    ".sct td{padding:8px;border-bottom:1px solid var(--line-soft);"
    "font-variant-numeric:tabular-nums;white-space:nowrap}"
    ".sct td.scn{font-weight:600}"
    ".sct td.ok{color:var(--gain)}.sct td.bd{color:var(--loss)}"
    ".scb{display:inline-block;padding:2px 9px;border-radius:100px;"
    "font-size:10.5px;font-weight:650;white-space:nowrap}"
    ".scb-ok{background:var(--gain-soft);color:var(--gain)}"
    ".scb-wn{background:var(--warn-soft);color:var(--warn)}"
    ".scb-p{background:var(--line-soft);color:var(--ink2)}"

    # ── Segmented control ────────────────────────────────────────────────────
    ".seg{display:inline-flex;gap:2px;background:var(--line-soft);"
    "border:1px solid var(--line-soft);border-radius:9px;padding:2px}"
    ".seg button,.seg a{border:0;background:transparent;font-size:12px;font-weight:600;"
    "color:var(--ink2);padding:4px 11px;border-radius:7px;cursor:pointer;"
    "font-family:inherit;transition:background .15s,color .15s;"
    "text-decoration:none;display:inline-flex;align-items:center}"
    ".seg button.on,.seg a.on{background:var(--surface);color:var(--ink);"
    "box-shadow:0 1px 3px rgba(0,0,0,.14)}"

    # ── Volatility monitor ───────────────────────────────────────────────────
    ".vm-head{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;"
    "padding:14px 18px 0}"
    ".vm-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}"
    ".selw{position:relative;display:inline-flex}"
    ".selw select{"
    "appearance:none;-webkit-appearance:none;font-family:inherit;font-size:16px;"
    "font-weight:600;letter-spacing:-.01em;color:var(--ink);background:var(--surface2);"
    "border:1px solid var(--line);border-radius:9px;padding:9px 32px 9px 12px;"
    "cursor:pointer}"
    ".selw::after{content:'';position:absolute;right:12px;top:50%;width:6px;height:6px;"
    "margin-top:-5px;border-right:1.6px solid var(--ink2);"
    "border-bottom:1.6px solid var(--ink2);transform:rotate(45deg);pointer-events:none}"
    ".vm-hero{margin-left:auto;text-align:right}"
    ".vm-now{font-size:2.1em;font-weight:650;letter-spacing:-.02em;line-height:1;"
    "font-variant-numeric:tabular-nums}"
    ".vm-state{display:inline-flex;align-items:center;margin-top:6px;"
    "font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;"
    "padding:3px 10px;border-radius:100px}"
    ".vm-exp{color:var(--loss);background:var(--loss-soft)}"
    ".vm-con{color:var(--gain);background:var(--gain-soft)}"
    ".vm-std{color:var(--ink2);background:var(--line-soft)}"
    ".vm-chartwrap{position:relative;margin:10px 18px 0}"
    ".vm-chartwrap canvas{display:block;width:100%;height:240px}"
    "@media (max-width:640px){.vm-chartwrap canvas{height:190px}"
    ".vm-hero{text-align:left;margin-left:0;width:100%}}"
    ".vm-tip{position:absolute;display:none;background:var(--ink);color:var(--bg);"
    "font-size:11.5px;font-weight:600;padding:4px 9px;border-radius:7px;"
    "transform:translate(-50%,-135%);pointer-events:none;white-space:nowrap;"
    "font-variant-numeric:tabular-nums;box-shadow:0 4px 14px rgba(0,0,0,.25)}"
    ".vm-foot{display:flex;gap:6px 18px;flex-wrap:wrap;align-items:center;"
    "padding:10px 18px 14px;font-size:12px;color:var(--ink2)}"
    ".vm-foot b{color:var(--ink);font-variant-numeric:tabular-nums;font-weight:600}"
    ".vm-legend{margin-left:auto;display:flex;gap:12px;font-size:11px;color:var(--ink3)}"
    ".vm-legend i{display:inline-block;width:14px;height:3px;border-radius:2px;"
    "margin-right:5px;vertical-align:middle}"

    # ── Bottom tab bar — primary nav (Home/Markets/Settings/More) ─────────────
    # Fixed on every screen size: the page lives mostly on an iPhone, so this
    # is the main way to move around; on desktop it just reads as a slim
    # footer nav. Frosted to match the topbar.
    ".tabbar{position:fixed;left:0;right:0;bottom:0;z-index:100;"
    "background:var(--glass);"
    "backdrop-filter:saturate(180%) blur(20px);"
    "-webkit-backdrop-filter:saturate(180%) blur(20px);"
    "border-top:1px solid var(--line-soft);"
    "padding-bottom:env(safe-area-inset-bottom)}"
    ".tabbar-in{max-width:640px;margin:0 auto;display:flex}"
    ".tb-item{flex:1;display:flex;flex-direction:column;align-items:center;"
    "justify-content:center;gap:2px;padding:7px 4px 6px;min-height:50px;"
    "color:var(--ink3);text-decoration:none;font-size:10px;font-weight:600;"
    "letter-spacing:.01em}"
    ".tb-item svg{width:22px;height:22px;stroke:currentColor;fill:none;"
    "stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}"
    ".tb-item.on{color:var(--accent)}"

    # ── Swipe carousel — /control's 4 panels ──────────────────────────────────
    # align-items:flex-start keeps each panel at its own natural height
    # instead of being stretched to the tallest sibling; _SWIPE_JS then pins
    # #swipe's own height to whichever panel is active (animated on change).
    ".swipe{display:flex;align-items:flex-start;overflow-x:auto;overflow-y:hidden;"
    "scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;"
    "scrollbar-width:none;transition:height .22s cubic-bezier(.25,.46,.45,.94)}"
    ".swipe::-webkit-scrollbar{display:none}"
    ".panel{flex:0 0 100%;min-width:0;scroll-snap-align:start}"

    # ── More panel — link-tiles to the rarely-used pages ──────────────────────
    ".morelinks{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));"
    "gap:10px}"
    ".mltile{background:var(--surface);border:1px solid var(--line-soft);"
    "border-radius:14px;padding:15px;text-decoration:none;color:var(--ink);"
    "display:flex;flex-direction:column;gap:4px;box-shadow:var(--shadow)}"
    ".mltile:active{background:var(--surface2)}"
    ".mltile b{font-size:14px;font-weight:650}"
    ".mltile span{font-size:11.5px;color:var(--ink3);line-height:1.4}"

    # ── Mobile / touch — this page's primary device is an iPhone ─────────────
    # No visual scaling here (that's the max-width:980px/640px rules above);
    # this is purely about the page behaving like a native app on a touch
    # screen: no accidental Safari zoom, no missed taps, no gray flash, and
    # content that respects the notch / home-indicator safe areas.
    "a,button,input,select,summary{-webkit-tap-highlight-color:transparent}"
    "@media (hover:none) and (pointer:coarse){"
    ".btn{min-height:44px;padding:11px 16px}"
    ".btn.bfull{min-height:48px}"
    ".fchip{min-height:36px;padding:8px 14px}"
    ".seg button{min-height:36px;padding:8px 13px}"
    ".tb-nav a{padding:10px 13px;min-height:44px;display:flex;align-items:center}"
    ".mdr,.pr2{min-height:44px}"
    ".vm-controls .selw select,.vm-controls .seg button{min-height:40px}"
    "}"
    "button,a.btn,.seg button,.fchip{touch-action:manipulation}"
    ".topbar{padding-top:env(safe-area-inset-top)}"
    ".wrap{padding-left:calc(16px + env(safe-area-inset-left));"
    "padding-right:calc(16px + env(safe-area-inset-right));"
    "padding-bottom:calc(84px + env(safe-area-inset-bottom))}"
)

# ── /control live script — shared by Home/Markets/Settings tabs ──────────────
# Live positions + hero MTM (30s), summary poll → meters/pills/next-job (30s,
# also drives the emergency-stop/paper/mode watch-and-reload), schedule-rail
# re-marking, commodity intelligence cards with inline approve/reject (5 min),
# and the Home intraday sparkline. Every DOM lookup is null-guarded, so a tab
# missing some of these elements (e.g. Markets has no #pos-wrap) just no-ops
# for that piece rather than erroring. The page seeds
# window.__realized/__estop/__paper/__mode/__snaps first.
_CONTROL_LIVE_JS = r"""
<script>
(function(){
'use strict';
function $(i){return document.getElementById(i)}
function tok(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}
var MTM=null;
function updateHero(){
var nEl=$('hero-net');if(!nEl)return;
var net=window.__realized+(MTM==null?0:MTM);
nEl.innerHTML=inr(net);
nEl.className='hero-pnl '+(net>0?'ok':(net<0?'bd':''))}
function inr(x){if(x==null)return '—';
var s=x>0?'+':(x<0?'−':'');
return s+'₹'+Math.abs(Math.round(x)).toLocaleString('en-IN')}
function num(x,d){return x==null?'—':Number(x).toFixed(d==null?1:d)}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){
return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function loadPositions(){
fetch('/commodity-agents/portfolio-greeks').then(function(r){
if(r.status===503)throw new Error('No Kite session — live greeks unavailable');
if(!r.ok)throw new Error('Live positions unavailable (HTTP '+r.status+')');
return r.json()}).then(function(d){
var w=document.getElementById('pos-wrap');if(!w)return;
if(!d.positions.length){
w.innerHTML='<div style="padding:13px 16px;font-size:.78em;color:#8E8E93">No open option positions.</div>';
}else{
var h='<table><thead><tr><th>Symbol</th><th>Side</th><th class="tr">Qty</th>'+
'<th class="tr">Entry</th><th class="tr">LTP</th><th class="tr">P&amp;L</th>'+
'<th class="tr">SL</th><th class="tr">Δ</th><th class="tr">Θ/day</th></tr></thead><tbody>';
d.positions.forEach(function(p){
var pc=p.pnl==null?'':(p.pnl>=0?' ok':' bd');
h+='<tr><td>'+esc(p.tradingsymbol)+'</td><td>'+esc(p.side)+'</td>'+
'<td class="tr">'+p.quantity+'</td><td class="tr">'+num(p.entry_price,2)+'</td>'+
'<td class="tr">'+num(p.ltp,2)+'</td>'+
'<td class="tr'+pc+'" style="font-weight:600">'+inr(p.pnl)+'</td>'+
'<td class="tr">'+num(p.sl,2)+'</td><td class="tr">'+num(p.delta,3)+'</td>'+
'<td class="tr">'+inr(p.theta_per_day)+'</td></tr>'});
h+='</tbody></table>';
var chips=Object.keys(d.straddles||{}).map(function(k){
var g=d.straddles[k];var bad=Math.abs(g.net_delta_per_lot)>=0.2;
return '<span class="pill '+(bad?'pr':'pg')+'">'+esc(g.underlying)+
' straddle Δ '+(g.net_delta_per_lot>0?'+':'')+g.net_delta_per_lot+'/lot</span>'}).join(' ');
var t=d.totals||{};var mg='';
if(d.margins){mg=Object.keys(d.margins).map(function(seg){
return seg+' ₹'+Math.round(d.margins[seg].net).toLocaleString('en-IN')+' free'}).join(' · ')}
h+='<div style="padding:10px 16px 13px;font-size:.74em;color:#636366">'+
(chips?chips+'<br>':'')+
'Book Δ '+num(t.net_delta_units)+' · vega '+num(t.net_vega)+
' · θ '+inr(t.net_theta_per_day)+'/day'+(mg?' · '+mg:'')+'</div>';
w.innerHTML=h;}
var t2=d.totals||{};
MTM=t2.open_mtm!=null?t2.open_mtm:(d.positions.length?null:0);
var mEl=$('hero-mtm');var tEl=$('hero-theta');
if(tEl)tEl.innerHTML=t2.net_theta_per_day!=null?inr(t2.net_theta_per_day)+'/day':'₹0/day';
if(mEl&&MTM!=null){mEl.innerHTML=inr(MTM);mEl.className=MTM>=0?'ok':'bd'}
updateHero();
}).catch(function(e){
var m=document.getElementById('pos-msg');if(m)m.textContent=e.message;});
}

/* ── summary poll: meters, kite pill, next job, rail, updated-at ── */
function inru(x){return '₹'+Math.abs(Math.round(x)).toLocaleString('en-IN')}
function barCls(p){return p>=100?'bbd':(p>=60?'bwn':'bok')}
function valCls(p){return p>=100?'bd':(p>=60?'wn':'ok')}
function setMeter(key,cur,max,money){
var b=$('m-'+key+'-b'),v=$('m-'+key+'-v');if(!b||!v)return;
var p=max>0?cur/max*100:0;
b.className='mb '+barCls(p);b.style.width=Math.min(p,100).toFixed(0)+'%';
v.className='mv '+valCls(p);
v.innerHTML=money?(inru(cur)+' / '+inru(max)):(cur+' / '+max)}
function remarkRail(){
var lis=document.querySelectorAll('.rail li[data-t]');
var now=new Date();var nowM=now.getHours()*60+now.getMinutes();
var nextFound=false;
lis.forEach(function(li){
if(li.getAttribute('data-en')!=='1')return;
var t=parseInt(li.getAttribute('data-t'),10);
var rs=li.querySelector('.rs');
if(t<=nowM){li.className='r-done';if(rs)rs.innerHTML='✓'}
else if(!nextFound){nextFound=true;li.className='r-next';if(rs)rs.innerHTML='● next'}
else{li.className='';if(rs)rs.innerHTML='–'}})}
function poll(){
fetch('/api/control/summary').then(function(r){if(!r.ok)throw 0;return r.json()})
.then(function(d){
if(d.emergency_stop!==window.__estop||d.paper!==window.__paper||
d.trade_mode!==window.__mode){location.reload();return}
window.__realized=d.realized;updateHero();
var rl=$('hero-real');if(rl){rl.innerHTML=inr(d.realized);
rl.className=d.realized>0?'ok':(d.realized<0?'bd':'')}
setMeter('loss',Math.round(d.today_loss),Math.round(d.max_loss),true);
setMeter('trades',d.trades_today,d.max_trades,false);
setMeter('pos',d.open_positions,d.max_positions,false);
setMeter('consec',d.consec_losses,d.consec_limit,false);
var k=$('pill-kite');if(k){k.className='pill '+(d.session_valid?'pg':'pr');
k.innerHTML='<span class="sdot"></span>Kite '+(d.session_valid?'OK':'INVALID')}
var nx=$('next-job');if(nx)nx.textContent=d.next_label?('next: '+d.next_label):'';
remarkRail();
var lut=$('lut');if(lut)lut.textContent='Updated '+new Date().toLocaleTimeString();
}).catch(function(){})}

/* ── commodity intelligence cards ── */
var CA='/commodity-agents';var caPending={};
function caBadge(rec){
if(!rec)return '<span class="pill pm">NO DATA</span>';
if(rec.risk_vetoed)return '<span class="pill pa">RISK VETO</span>';
var cls=rec.direction==='SELL'?'pr':(rec.direction==='BUY'?'pg':'pm');
var txt=rec.direction==='NO_TRADE'?'NO TRADE':rec.direction;
if(rec.status&&rec.status!=='PROPOSED')txt+=' · '+rec.status;
return '<span class="pill '+cls+'">'+esc(txt)+'</span>'}
function caMsg(m){var e=$('ca-msg');if(e){e.textContent=m;
setTimeout(function(){e.textContent=''},6000)}}
window.caDecide=function(id,action,btn){
var body={recommendation_id:id,action:action};
var p=caPending[id];
if(p&&p.action===action){body.confirm_token=p.token;body.lots=parseInt(p.lots,10)||1}
fetch(CA+'/decision',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify(body)})
.then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j}})})
.then(function(res){
if(!res.ok){caMsg(res.j.detail||'error');return}
if(res.j.status==='confirm_required'){
caPending[id]={action:action,token:res.j.confirm_token,
lots:btn.getAttribute('data-lots')||1};
btn.textContent='CONFIRM '+action.toUpperCase();return}
delete caPending[id];
caMsg(action.toUpperCase()+' recorded — '+res.j.status+
(res.j.note?' ('+res.j.note+')':''));
loadCommodities()}).catch(function(){caMsg('network error')})};
function renderCA(coms,d,ivs){
var g=$('ca-grid');if(!g)return;var out='';
coms.forEach(function(c,i){
var rec=(d.recommendations||{})[c];var iv=ivs[i];
var last=(iv&&iv.points&&iv.points.length)?iv.points[iv.points.length-1]:null;
var tr=iv&&iv.iv_trend;var arrow='',trTxt='';
if(tr&&tr.direction==='expanding'){arrow=' ▲';trTxt='expanding'}
else if(tr&&tr.direction==='contracting'){arrow=' ▼';trTxt='contracting'}
else if(tr&&tr.direction==='stable'){arrow=' →';trTxt='stable'}
var kv='<div class="cakv">'+
'<span>ATM IV</span><span>'+(last?num(last.iv*100,1)+'%'+arrow:'—')+'</span>'+
(trTxt?'<span>IV trend</span><span>'+trTxt+
(tr.change_pct!=null?' '+(tr.change_pct>0?'+':'')+tr.change_pct+'%':'')+'</span>':'')+
'<span>VRP</span><span>'+(last&&last.vrp!=null?
((last.vrp>0?'+':'')+num(last.vrp*100,1)+' pts'):'—')+'</span>'+
(rec&&rec.confidence!=null?'<span>Confidence</span><span>'+
Math.round(rec.confidence*100)+'%</span>':'')+
(rec&&rec.suggested_lots!=null?'<span>Size</span><span>'+rec.suggested_lots+
' lot'+(rec.suggested_lots===1?'':'s')+'</span>':'')+
'</div>';
var btns='<div class="cabtns">';
if(rec&&rec.status==='PROPOSED'&&!rec.risk_vetoed&&rec.direction!=='NO_TRADE'){
btns+='<button class="btn bg2" data-lots="'+(rec.suggested_lots||1)+
'" onclick="caDecide('+rec.id+',\'approve\',this)">Approve</button>'+
'<button class="btn br2" onclick="caDecide('+rec.id+',\'reject\',this)">Reject</button>'}
btns+='<a class="btn bm" style="text-decoration:none" href="'+CA+'/analyze?t='+c+'">Analyze</a></div>';
var summary=(rec&&rec.reasoning_summary)?
'<div style="font-size:.7em;color:#636366;margin-top:6px">'+
esc(rec.reasoning_summary.slice(0,140))+'</div>':'';
out+='<div class="cacard"><div class="cah"><b>'+esc(c)+'</b>'+caBadge(rec)+'</div>'+
kv+summary+btns+'</div>'});
g.innerHTML=out||'<div style="font-size:.78em;color:#8E8E93">No commodities configured.</div>'}
function loadCommodities(){
if(!$('ca-grid'))return;
fetch(CA+'/recommendations').then(function(r){if(!r.ok)throw 0;return r.json()})
.then(function(d){
var coms=Object.keys(d.recommendations||{});
Promise.all(coms.map(function(c){
return fetch(CA+'/'+c+'/iv-history?limit=40')
.then(function(r){return r.ok?r.json():null}).catch(function(){return null})}))
.then(function(ivs){renderCA(coms,d,ivs)})})
.catch(function(){var g=$('ca-grid');
if(g)g.innerHTML='<div style="font-size:.78em;color:#8E8E93">Commodity agents unavailable.</div>'})}

/* ── intraday sparkline (Today card) ── */
function daySpark(){
var box=$('day-spark');var s=window.__snaps||[];
if(!box||s.length<2)return;
var w=460,h=52,vals=s.map(function(p){return p[1]});
var min=Math.min.apply(null,vals.concat([0])),max=Math.max.apply(null,vals.concat([0]));
if(max===min)max=min+1;
var X=function(i){return 4+i*(w-58)/(vals.length-1)};
var Y=function(v){return h-5-(v-min)/(max-min)*(h-10)};
var pp=vals.map(function(v,i){return X(i).toFixed(1)+','+Y(v).toFixed(1)}).join(' ');
var lastV=vals[vals.length-1];
var col=lastV>=0?tok('--gain-b'):tok('--loss-b');
var svg='<svg viewBox="0 0 '+w+' '+h+'" style="width:100%;height:'+h+'px;display:block">';
if(min<0&&max>0)svg+='<line x1="4" y1="'+Y(0).toFixed(1)+'" x2="'+(w-54)+
'" y2="'+Y(0).toFixed(1)+'" stroke="'+tok('--line')+'" stroke-dasharray="3 3"/>';
svg+='<polyline points="'+pp+'" fill="none" stroke="'+col+
'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
var lx=X(vals.length-1),ly=Y(lastV);
svg+='<circle cx="'+lx.toFixed(1)+'" cy="'+ly.toFixed(1)+'" r="3" fill="'+col+'"/>';
svg+='<text x="'+(lx+7).toFixed(1)+'" y="'+(ly+3.5).toFixed(1)+
'" style="font-size:10px;fill:'+tok('--ink2')+'">'+inr(lastV)+'</text></svg>';
box.innerHTML=svg}

loadPositions();setInterval(loadPositions,30000);
poll();setInterval(poll,30000);
loadCommodities();setInterval(loadCommodities,300000);
daySpark();remarkRail();setInterval(remarkRail,60000);
if(window.matchMedia)
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',
function(){daySpark()});
})();
</script>
"""

# ── /history live script — equity curve + activity-feed filters ──────────────
# Split out of _CONTROL_LIVE_JS: these two elements moved to the History tab
# (Performance/Scorecard/Activity review), so Home no longer needs them and
# History doesn't need Home's positions/summary-poll/commodity-agents fetches.
# Page seeds window.__perf first.
_HISTORY_LIVE_JS = r"""
<script>
(function(){
'use strict';
function $(i){return document.getElementById(i)}
function tok(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}
function inr(x){if(x==null)return '—';
var s=x>0?'+':(x<0?'−':'');
return s+'₹'+Math.abs(Math.round(x)).toLocaleString('en-IN')}

/* ── equity curve (Performance card) ── */
function eqChart(){
var box=$('eq-chart');if(!box)return;
var days=(window.__perf||{}).days||[];
if(days.length<2){box.innerHTML='<div style="padding:0 16px 14px;font-size:.76em;'+
'color:#8E8E93">Not enough closed trades yet — the curve appears after a few sessions.</div>';return}
var cum=[];var c=0;
days.forEach(function(d){c+=d[1];cum.push(c)});
var w=560,h=180,pl=46,pr=14,pt=10,pb=20;
var min=Math.min.apply(null,cum.concat([0])),max=Math.max.apply(null,cum.concat([0]));
if(max===min)max=min+1;
var X=function(i){return pl+i*(w-pl-pr)/(cum.length-1)};
var Y=function(v){return pt+(1-(v-min)/(max-min))*(h-pt-pb)};
var NS='http://www.w3.org/2000/svg';
function mk(n,a){var e=document.createElementNS(NS,n);
for(var k in a)e.setAttribute(k,a[k]);return e}
var svg=mk('svg',{viewBox:'0 0 '+w+' '+h});
svg.style.width='100%';svg.style.display='block';
var ACC=tok('--accent');
for(var i=0;i<=4;i++){
var gv=min+(max-min)*i/4;
svg.appendChild(mk('line',{x1:pl,y1:Y(gv),x2:w-pr,y2:Y(gv),stroke:tok('--line-soft')}));
var t=mk('text',{x:pl-6,y:Y(gv)+3.5,'text-anchor':'end'});
t.style.cssText='font-size:9.5px;fill:'+tok('--ink3');
t.textContent=Math.abs(gv)>=1000?(gv/1000).toFixed(1)+'k':Math.round(gv);
svg.appendChild(t)}
if(min<0&&max>0)svg.appendChild(mk('line',{x1:pl,y1:Y(0),x2:w-pr,y2:Y(0),stroke:tok('--line')}));
var line=cum.map(function(v,i){return X(i).toFixed(1)+','+Y(v).toFixed(1)}).join(' ');
var y0=Y(Math.max(min,0));
svg.appendChild(mk('polygon',{points:pl+','+y0+' '+line+' '+X(cum.length-1)+','+y0,
fill:ACC,'fill-opacity':'.10'}));
svg.appendChild(mk('polyline',{points:line,fill:'none',stroke:ACC,
'stroke-width':2,'stroke-linejoin':'round'}));
var li=cum.length-1;
svg.appendChild(mk('circle',{cx:X(li),cy:Y(cum[li]),r:3.5,fill:ACC}));
var lt=mk('text',{x:X(li)-6,y:Y(cum[li])-8,'text-anchor':'end'});
lt.style.cssText='font-size:10.5px;font-weight:700;fill:'+tok('--ink');
lt.textContent=inr(cum[li]);svg.appendChild(lt);
var MO=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
[0,Math.floor(days.length/2),days.length-1].forEach(function(ix){
var t2=mk('text',{x:X(ix),y:h-5,
'text-anchor':ix===0?'start':(ix===days.length-1?'end':'middle')});
t2.style.cssText='font-size:9.5px;fill:'+tok('--ink3');
var dd=new Date(days[ix][0]);
t2.textContent=dd.getDate()+' '+MO[dd.getMonth()];
svg.appendChild(t2)});
var tip=$('eq-tip');
if(!tip){tip=document.createElement('div');tip.id='eq-tip';
tip.style.cssText='position:fixed;pointer-events:none;background:var(--surface);'+
'border:1px solid var(--line);border-radius:7px;padding:5px 9px;'+
'font-size:12px;color:var(--ink);box-shadow:var(--shadow);'+
'display:none;z-index:50;font-variant-numeric:tabular-nums';
document.body.appendChild(tip)}
var cross=mk('line',{y1:pt,y2:h-pb,stroke:tok('--line'),visibility:'hidden'});
var dot=mk('circle',{r:3.5,fill:ACC,visibility:'hidden'});
svg.appendChild(cross);svg.appendChild(dot);
svg.addEventListener('mousemove',function(ev){
var r=svg.getBoundingClientRect();
var fx=(ev.clientX-r.left)/r.width*w;
var i2=Math.round((fx-pl)/((w-pl-pr)/(cum.length-1)));
i2=Math.max(0,Math.min(cum.length-1,i2));
cross.setAttribute('x1',X(i2));cross.setAttribute('x2',X(i2));
cross.setAttribute('visibility','visible');
dot.setAttribute('cx',X(i2));dot.setAttribute('cy',Y(cum[i2]));
dot.setAttribute('visibility','visible');
tip.style.display='block';tip.style.left=(ev.clientX+12)+'px';
tip.style.top=(ev.clientY-8)+'px';
tip.innerHTML=days[i2][0]+' · day '+inr(days[i2][1])+' · cum '+inr(cum[i2])});
svg.addEventListener('mouseleave',function(){
cross.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');
tip.style.display='none'});
box.innerHTML='';box.appendChild(svg)}

/* ── activity-feed filters ── */
document.querySelectorAll('.fchip').forEach(function(ch){
ch.addEventListener('click',function(){
document.querySelectorAll('.fchip').forEach(function(x){x.classList.remove('on')});
ch.classList.add('on');
var f=ch.getAttribute('data-f');
document.querySelectorAll('#feed li').forEach(function(li){
li.style.display=(f==='all'||li.getAttribute('data-k')===f)?'':'none'})})});

eqChart();
if(window.matchMedia)
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',function(){eqChart()});
})();
</script>
"""

# ── Volatility monitor — big trend-coloured ATM-IV chart on /control ─────────
# Data: /commodity-agents/{scrip}/iv-history (30-min agent samples). The line
# is coloured by an EMA-slope hysteresis: red while IV expands (hurts short
# straddles), green while it contracts. Scrip + range persist in localStorage.
_VOL_MONITOR_JS = r"""
<script>
(function(){
'use strict';
var canvas=document.getElementById('vm-chart');if(!canvas)return;
var ctx=canvas.getContext('2d');
var tip=document.getElementById('vm-tip');
var CA='/commodity-agents';
var MO=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
var cache={};
var cur=localStorage.getItem('vm-scrip')||'NATURALGAS';
var rangeDays=parseInt(localStorage.getItem('vm-range')||'7',10);
var view={pts:[],X:null,Y:null,padL:46,w:0};
function $(i){return document.getElementById(i)}
function tok(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}
function msg(t){var e=$('vm-msg');if(e)e.textContent=t||''}
function fetchSeries(scrip,cb){
var c=cache[scrip];
if(c&&Date.now()-c.at<240000){cb(c.pts);return}
fetch(CA+'/'+scrip+'/iv-history?limit=500')
.then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()})
.then(function(d){
var pts=(d.points||[]).map(function(p){return{
t:new Date(p.t),iv:p.iv*100,
rv:p.rv==null?null:p.rv*100,vrp:p.vrp==null?null:p.vrp*100}})
.filter(function(p){return isFinite(p.iv)&&!isNaN(p.t.getTime())});
cache[scrip]={pts:pts,at:Date.now()};msg('');cb(pts)})
.catch(function(){msg('IV history unavailable');cb((c&&c.pts)||[])})}
/* trend state per point: EMA slope + hysteresis -> clean runs, no flicker */
function trendStates(pts){
var st=new Array(pts.length).fill(0);
if(pts.length<3)return st;
var difs=[];for(var i=1;i<pts.length;i++)difs.push(pts[i].iv-pts[i-1].iv);
var m=0;difs.forEach(function(d){m+=d*d});
var thr=Math.min(.5,Math.max(.015,.45*Math.sqrt(m/difs.length)));
var ema=0,curSt=0;
for(var j=1;j<pts.length;j++){
ema=.25*(pts[j].iv-pts[j-1].iv)+.75*ema;
if(ema>thr)curSt=1;else if(ema<-thr)curSt=-1;
st[j]=curSt}
return st}
function visible(all){
if(rangeDays>=99||!all.length)return all;
var cutoff=all[all.length-1].t.getTime()-rangeDays*86400000;
return all.filter(function(p){return p.t.getTime()>=cutoff})}
function fmtHM(d){var h=d.getHours(),m=d.getMinutes();
return (h<10?'0':'')+h+':'+(m<10?'0':'')+m}
function draw(all){
var wrap=canvas.parentElement;
var dpr=window.devicePixelRatio||1;
var W=wrap.clientWidth,H=canvas.clientHeight||240;
canvas.width=W*dpr;canvas.height=H*dpr;
ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
var pts=visible(all);
view={pts:pts,X:null,Y:null,padL:46,w:W};
if(pts.length<2){
ctx.font='600 12px -apple-system,sans-serif';ctx.fillStyle=tok('--ink3');
ctx.textAlign='center';ctx.fillText('Not enough IV samples yet',W/2,H/2);
updateStats(all,pts,[]);return}
var st=trendStates(pts);
var pad={l:46,r:14,t:12,b:24};
var iw=W-pad.l-pad.r,ih=H-pad.t-pad.b;
var lo=Infinity,hi=-Infinity;
pts.forEach(function(p){if(p.iv<lo)lo=p.iv;if(p.iv>hi)hi=p.iv});
var span=Math.max(.6,hi-lo);lo-=span*.1;hi+=span*.1;
var t0=pts[0].t.getTime(),t1=pts[pts.length-1].t.getTime();
if(t1===t0)t1=t0+1;
var X=function(i){return pad.l+(pts[i].t.getTime()-t0)/(t1-t0)*iw};
var Y=function(v){return pad.t+(1-(v-lo)/(hi-lo))*ih};
view.X=X;view.Y=Y;
var cGrid=tok('--line-soft'),cInk3=tok('--ink3');
var cGain=tok('--gain-b'),cLoss=tok('--loss-b'),cNeut=tok('--ink3');
/* y grid + % labels */
ctx.font='600 10.5px -apple-system,sans-serif';
ctx.textAlign='right';ctx.textBaseline='middle';
var stp=(hi-lo)>=8?2:((hi-lo)>=3?1:.5);
for(var v=Math.ceil(lo/stp)*stp;v<=hi;v+=stp){
var y=Y(v);
ctx.strokeStyle=cGrid;ctx.lineWidth=1;
ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();
ctx.fillStyle=cInk3;
ctx.fillText(v.toFixed(stp<1?1:0)+'%',pad.l-7,y)}
/* x labels: hours for 1D, day starts otherwise */
ctx.textAlign='center';ctx.textBaseline='top';ctx.fillStyle=cInk3;
if(rangeDays<=1){
var step=Math.max(1,Math.floor(pts.length/6));
for(var i2=0;i2<pts.length;i2+=step)
ctx.fillText(fmtHM(pts[i2].t),X(i2),H-pad.b+7)}
else{
var lastDay=null,dayIdx=0;
var skip=rangeDays>=99?2:1;
for(var i3=0;i3<pts.length;i3++){
var dk=pts[i3].t.getFullYear()+'-'+pts[i3].t.getMonth()+'-'+pts[i3].t.getDate();
if(dk!==lastDay){
lastDay=dk;dayIdx++;
if(i3>0){ctx.strokeStyle=cGrid;
ctx.beginPath();ctx.moveTo(X(i3),pad.t);ctx.lineTo(X(i3),H-pad.b);ctx.stroke()}
if(dayIdx%skip===0)
ctx.fillText(pts[i3].t.getDate()+' '+MO[pts[i3].t.getMonth()],X(i3),H-pad.b+7)}}}
/* one quiet area fill under the whole curve */
ctx.beginPath();ctx.moveTo(X(0),H-pad.b);
for(var j=0;j<pts.length;j++)ctx.lineTo(X(j),Y(pts[j].iv));
ctx.lineTo(X(pts.length-1),H-pad.b);ctx.closePath();
ctx.globalAlpha=.05;ctx.fillStyle=tok('--ink');ctx.fill();ctx.globalAlpha=1;
/* trend-coloured line segments */
function col(i){return st[i]===1?cLoss:(st[i]===-1?cGain:cNeut)}
var i0=0;
for(var k=1;k<=pts.length;k++){
if(k===pts.length||col(k)!==col(i0)){
var end=Math.min(k,pts.length-1);
ctx.beginPath();ctx.moveTo(X(i0),Y(pts[i0].iv));
for(var q=i0+1;q<=end;q++)ctx.lineTo(X(q),Y(pts[q].iv));
ctx.strokeStyle=col(i0);ctx.lineWidth=2.2;
ctx.lineJoin='round';ctx.lineCap='round';ctx.stroke();
i0=k}}
/* endpoint */
var li=pts.length-1,lx=X(li),ly=Y(pts[li].iv),lc=col(li);
ctx.globalAlpha=.22;
ctx.beginPath();ctx.arc(lx,ly,8,0,Math.PI*2);ctx.fillStyle=lc;ctx.fill();
ctx.globalAlpha=1;
ctx.beginPath();ctx.arc(lx,ly,3.5,0,Math.PI*2);ctx.fillStyle=lc;ctx.fill();
updateStats(all,pts,st)}
function updateStats(all,pts,st){
var now=$('vm-now'),stEl=$('vm-state');
if(!pts.length){
if(now)now.innerHTML='&mdash;';
if(stEl){stEl.className='vm-state vm-std';stEl.textContent='no data'}
['vm-open','vm-rangev','vm-rv','vm-vrp'].forEach(function(id){
var e=$(id);if(e)e.innerHTML='&mdash;'});
return}
var last=pts[pts.length-1];
if(now)now.textContent=last.iv.toFixed(1)+'%';
/* reference = newest sample at least 24h old (MCX sessions span midnight) */
var refT=last.t.getTime()-86400000,d0=0;
for(var r=pts.length-1;r>=0;r--){if(pts[r].t.getTime()<=refT){d0=r;break}}
var open=pts[d0].iv,delta=last.iv-open;
var lo=Infinity,hi=-Infinity;
all.forEach(function(p){if(p.iv<lo)lo=p.iv;if(p.iv>hi)hi=p.iv});
var e;
if((e=$('vm-open')))e.textContent=open.toFixed(1)+'%';
if((e=$('vm-rangev')))e.textContent=lo.toFixed(1)+' – '+hi.toFixed(1)+'%';
if((e=$('vm-rv')))e.textContent=last.rv==null?'—':last.rv.toFixed(1)+'%';
if((e=$('vm-vrp')))e.textContent=last.vrp==null?'—':
((last.vrp>=0?'+':'−')+Math.abs(last.vrp).toFixed(1)+' pts');
var s=st.length?st[st.length-1]:0;
var dTxt=(delta>=0?'+':'−')+Math.abs(delta).toFixed(1)+' pts / 24h';
if(stEl){
if(s===1){stEl.className='vm-state vm-exp';stEl.textContent='Expanding · '+dTxt}
else if(s===-1){stEl.className='vm-state vm-con';stEl.textContent='Contracting · '+dTxt}
else{stEl.className='vm-state vm-std';stEl.textContent='Steady · '+dTxt}}}
/* hover readout */
canvas.addEventListener('pointermove',function(ev){
if(!view.pts.length||!view.X)return;
var r=canvas.getBoundingClientRect();
var mx=ev.clientX-r.left;
var best=0,bd=Infinity;
for(var i=0;i<view.pts.length;i++){
var d=Math.abs(view.X(i)-mx);if(d<bd){bd=d;best=i}}
var p=view.pts[best];
tip.style.display='block';
tip.style.left=view.X(best)+'px';
tip.style.top=view.Y(p.iv)+'px';
tip.textContent=p.t.getDate()+' '+MO[p.t.getMonth()]+' '+fmtHM(p.t)+' · '+p.iv.toFixed(1)+'%'});
canvas.addEventListener('pointerleave',function(){tip.style.display='none'});
/* controls */
var sel=$('vm-scrip');
if(sel){
sel.value=cur;
if(sel.value!==cur){cur='NATURALGAS';sel.value=cur}
sel.addEventListener('change',function(){
cur=sel.value;localStorage.setItem('vm-scrip',cur);load()})}
var seg=$('vm-range');
if(seg){
seg.querySelectorAll('button').forEach(function(b){
b.classList.toggle('on',parseInt(b.getAttribute('data-d'),10)===rangeDays)});
seg.addEventListener('click',function(ev){
var b=ev.target.closest('button');if(!b)return;
seg.querySelectorAll('button').forEach(function(x){x.classList.remove('on')});
b.classList.add('on');
rangeDays=parseInt(b.getAttribute('data-d'),10);
localStorage.setItem('vm-range',String(rangeDays));
load()})}
function load(){fetchSeries(cur,draw)}
var raf=null;
function redraw(){if(raf)cancelAnimationFrame(raf);
raf=requestAnimationFrame(function(){var c=cache[cur];if(c)draw(c.pts)})}
if(window.ResizeObserver)new ResizeObserver(redraw).observe(canvas.parentElement);
if(window.matchMedia)
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',redraw);
load();setInterval(load,300000);
})();
</script>
"""

# ── Quick Trade panel — injected at top of /control ───────────────────────────
_QUICK_TRADE_PANEL = """
<div class="card" id="qt-panel">
<div class="ct">Quick Trade &#x26A1;</div>
<div id="qt-input-area" style="padding:16px">
<textarea id="qt-text" rows="2" style="width:100%;font-size:17px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;resize:none;font-family:inherit;background:var(--surface2);color:var(--ink);-webkit-appearance:none;transition:border-color .2s;margin-bottom:10px;display:block" placeholder="Type or dictate your trade &#x2014; e.g. buy one lot NIFTY ATM call"></textarea>
<div id="qt-err" style="display:none;background:var(--loss-soft);color:var(--loss);border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:10px"></div>
<button id="qt-parse-btn" class="btn bp bfull">Parse Trade</button>
</div>
<div id="qt-confirm" style="display:none;padding:16px;border-top:1px solid var(--line-soft)">
<div id="qt-summary" style="font-size:17px;font-weight:600;color:var(--ink);line-height:1.5;margin-bottom:8px"></div>
<div id="qt-confidence" style="font-size:.82em;color:var(--ink2);margin-bottom:10px"></div>
<div id="qt-low-conf-warn" style="display:none;background:var(--warn-soft);color:var(--warn);border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px">&#x26A0;&#xFE0F; Low confidence parse &#x2014; review carefully before executing</div>
<div id="qt-double-warn" style="display:none;background:var(--loss-soft);color:var(--loss);border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px">&#x26A0;&#xFE0F; EXIT / SQUARE-OFF detected &#x2014; this will close positions. Double-check.</div>
<div id="qt-limit-info" style="display:none;background:var(--accent-soft);color:var(--accent);border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px"></div>
<div id="qt-straddle-report" style="display:none;background:var(--surface2);border:1px solid var(--line-soft);border-radius:8px;padding:10px 12px;font-size:.78em;font-family:'SF Mono',Menlo,Consolas,monospace;white-space:pre-wrap;color:var(--ink);margin-bottom:8px;line-height:1.55"></div>
<div id="qt-straddle-warn" style="display:none;background:var(--warn-soft);color:var(--warn);border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px">&#x26A0;&#xFE0F; Spread threshold exceeded &#x2014; tap Force Execute to proceed anyway, or Cancel.</div>
<div id="qt-confirm-err" style="display:none;background:var(--loss-soft);color:var(--loss);border-radius:8px;padding:9px 12px;font-size:.82em;font-weight:500;margin-bottom:8px"></div>
<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
<button id="qt-exec-btn" class="btn bg2" style="flex:1;min-width:130px;padding:14px;font-size:.9em">Execute Trade</button>
<button id="qt-cancel-btn" class="btn bm" style="flex:1;min-width:100px;padding:14px;font-size:.9em">Cancel</button>
</div>
<div style="display:flex;align-items:center;justify-content:space-between;min-height:28px">
<div id="qt-timer" style="font-size:.76em;color:var(--ink3);font-variant-numeric:tabular-nums"></div>
<button id="qt-reparse-btn" class="btn bn" style="display:none;font-size:.76em;padding:6px 12px">Re-parse &#x21BA;</button>
</div>
</div>
<div id="qt-result" style="display:none;padding:16px;border-top:1px solid var(--line-soft)"></div>
<div style="border-top:1px solid var(--line-soft);padding:12px 16px;display:flex;align-items:center;justify-content:space-between">
<div id="qt-channel-status" style="font-size:.8em;color:var(--ink3)">Checking channel&#x2026;</div>
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
    e.innerHTML='<div style="background:var(--gain-soft);border-radius:10px;padding:14px;color:var(--gain);font-weight:600">'
      +'✅ Trade Executed'
      +(lines.length?'<div style="font-weight:400;font-size:.82em;margin-top:5px;color:var(--ink2)">'+lines.join(' · ')+'</div>':'')
      +'</div>';
  }else if(type==='cancel'){
    e.innerHTML='<div style="background:var(--line-soft);border-radius:10px;padding:14px;color:var(--ink2);font-weight:500">↩ Trade Cancelled</div>';
  }else{
    e.innerHTML='<div style="background:var(--loss-soft);border-radius:10px;padding:14px;color:var(--loss);font-weight:500">⚠ '+(data||'Error')+'</div>';
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

# ── Swipe carousel — /control's 4 tabs (Home/Markets/Settings/More) ──────────
# Panels sit side by side in #swipe (CSS scroll-snap does the touch physics —
# momentum, rubber-banding, snapping — natively, which feels far better on
# iOS Safari than a hand-rolled drag handler). Panels differ in height, so
# JS keeps #swipe's own height pinned to whichever panel is active — on tab
# click, on swipe-end, and via ResizeObserver (covers async content like the
# live positions table changing the Home panel's height after first paint).
# Tab bar links are plain /control#<tab> hrefs (works as a real navigation
# from any other page); this script only intercepts the click when already
# on /control, and restores the last-viewed tab from the URL hash or
# sessionStorage so a toggle's page reload doesn't bounce back to Home.
_SWIPE_JS = r"""
<script>
(function(){
'use strict';
var swipe=document.getElementById('swipe');if(!swipe)return;
var panels=Array.prototype.slice.call(swipe.children);
var names=panels.map(function(p){return p.id});
var tabs=Array.prototype.slice.call(document.querySelectorAll('.tb-item[data-tab]'));

function idxOf(name){var i=names.indexOf(name);return i<0?0:i}
function activeIdx(){
  return Math.max(0,Math.min(panels.length-1,Math.round(swipe.scrollLeft/swipe.clientWidth)));
}
function setHeight(i){var p=panels[i];if(p)swipe.style.height=p.scrollHeight+'px'}
function markActive(i){
  tabs.forEach(function(t){t.classList.toggle('on',t.getAttribute('data-tab')===names[i])});
}
function persist(i){
  try{sessionStorage.setItem('zb_tab',names[i]);}catch(e){}
  history.replaceState(null,'','#'+names[i]);
}
function syncTo(i){markActive(i);setHeight(i);persist(i)}
function sync(){syncTo(activeIdx())}
function goTo(i){swipe.scrollTo({left:i*swipe.clientWidth,behavior:'smooth'})}

tabs.forEach(function(t){
  t.addEventListener('click',function(e){
    var i=idxOf(t.getAttribute('data-tab'));
    if(!panels[i])return; // not on /control — let the link navigate there
    e.preventDefault();
    // instant feedback — tab/height/persisted-tab update immediately rather
    // than waiting for the scroll-end handler, so a tap always feels snappy
    // even if the scroll animation itself is slow or interrupted
    syncTo(i);
    goTo(i);
  });
});

var scrollTimer=null;
swipe.addEventListener('scroll',function(){
  if(scrollTimer)clearTimeout(scrollTimer);
  scrollTimer=setTimeout(sync,80);
},{passive:true});

// keep the same panel aligned across orientation change / resize
var lastW=swipe.clientWidth;
window.addEventListener('resize',function(){
  var i=activeIdx();
  if(swipe.clientWidth!==lastW){lastW=swipe.clientWidth;swipe.scrollLeft=i*swipe.clientWidth;}
  setHeight(i);
});

if(window.ResizeObserver){
  var ro=new ResizeObserver(function(){setHeight(activeIdx())});
  panels.forEach(function(p){ro.observe(p)});
}

// restore last-viewed tab: URL hash wins, else last session, else Home
var start=(location.hash||'').slice(1);
if(names.indexOf(start)<0){
  try{start=sessionStorage.getItem('zb_tab')||'';}catch(e){start=''}
}
var startIdx=names.indexOf(start)<0?0:idxOf(start);
swipe.scrollLeft=startIdx*swipe.clientWidth;
markActive(startIdx);
setHeight(startIdx);
})();
</script>
"""


def _fts(ts: datetime) -> datetime:
    """Attach IST to a naive timestamp so cross-timezone comparisons are safe."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=IST)


def _status_strip(settings: Settings) -> tuple[str, str]:
    """Emergency-stop banner + annunciator pill strip — shown on every page.

    Pure in-memory state/settings reads (no DB session needed), so _shell()
    can call this for every route without threading a session through.
    """
    from app.voice.config import is_voice_enabled

    estop = state.is_emergency_stop()
    paper = _dry_run(settings)
    trade_mode = state.get_trade_mode()
    overrides = state.get_all_overrides()

    try:
        sess_valid = bool(get_session_manager().get_token_info()["is_valid"])
    except Exception:
        sess_valid = False

    mode_pill = "pg" if trade_mode == "BUY_OPTIONS" else ("pr" if trade_mode == "SELL_OPTIONS" else "pa")
    paper_pill = "pa" if paper else "pp"
    paper_label = "PAPER MODE" if paper else "LIVE TRADING"
    sess_pill = "pg" if sess_valid else "pr"

    def spill(label: str, on: bool) -> str:
        return (f"<span class='pill {'pg' if on else 'pm'}'><span class='sdot'></span>"
                f"{label} {'ON' if on else 'OFF'}</span>")

    _ovr_keys = [
        "max_lots", "max_daily_loss", "sl_pct", "rr_ratio", "daily_profit_target",
        "sell_options_profit_pct", "entry_window_start", "entry_window_end",
        "no_entry_on_expiry_day", "max_trades_per_day", "max_open_positions",
        "capital_per_trade", "consecutive_losses_limit", "adx_threshold",
    ]
    n_ovr = sum(1 for k in _ovr_keys if overrides.get(k) is not None)

    strip_html = (
        "<div class='strip'>"
        + f"<span class='pill {paper_pill}'><span class='sdot'></span>{paper_label}</span>"
        + f"<span class='pill {sess_pill}' id='pill-kite'><span class='sdot'></span>Kite {'OK' if sess_valid else 'INVALID'}</span>"
        + f"<span class='pill {mode_pill}'><span class='sdot'></span>{trade_mode.replace('_', ' ')}</span>"
        + spill("Trailing", state.is_trailing_enabled())
        + spill("Window straddle", state.is_window_straddle_enabled())
        + spill("NG hedge", state.is_ng_hedge_enabled(settings.NG_DELTA_HEDGE_ENABLED))
        + spill("Defense", state.is_straddle_defense_enabled(settings.STRADDLE_DEFENSE_ENABLED))
        + spill("Sched. straddle", settings.SCHEDULED_STRADDLE_ENABLED)
        + spill("Voice", is_voice_enabled())
        + (f"<span class='pill pa'><span class='sdot'></span>{n_ovr} overrides</span>" if n_ovr else "")
        + "</div>"
    )
    stop_banner = (
        "<div class='sbanner'>&#x26D4; EMERGENCY STOP ACTIVE &mdash; No new trades will execute</div>"
        if estop else ""
    )
    return stop_banner, strip_html


def _activity_feed_html(session: Session) -> str:
    """Alerts / orders / GTTs / exits / errors in the last 48h — History tab."""
    feed_cutoff = datetime.now(IST) - timedelta(hours=48)

    def _hesc(s: str | None) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    feed: list[tuple[datetime, str, str]] = []
    for a in (session.query(Alert).filter(Alert.received_at >= feed_cutoff)
              .order_by(Alert.received_at.desc()).limit(30)):
        feed.append((_fts(a.received_at), "alert",
                     f"{_hesc(a.action)} {_hesc(a.tv_ticker)}"
                     f" <span class='fdim'>&middot; {_hesc(a.strategy_id)}</span>"))
    for o in (session.query(Order).filter(Order.placed_at >= feed_cutoff)
              .order_by(Order.placed_at.desc()).limit(30)):
        fill = f" @ {o.fill_price:.2f}" if o.fill_price else ""
        feed.append((_fts(o.placed_at), "order",
                     f"{_hesc(o.transaction_type)} {_hesc(o.tradingsymbol)} &times;{o.quantity}"
                     f" <span class='fdim'>&middot; {_hesc(o.status)}{fill}</span>"))
    for g in (session.query(Gtt).filter(Gtt.placed_at >= feed_cutoff)
              .order_by(Gtt.placed_at.desc()).limit(30)):
        feed.append((_fts(g.placed_at), "gtt",
                     f"OCO {_hesc(g.tradingsymbol)} <span class='fdim'>&middot; "
                     f"SL {g.sl_trigger:g} / tgt {g.target_trigger:g}"
                     f" &middot; {_hesc(g.status)}</span>"))
    for t in (session.query(ClosedTrade).filter(ClosedTrade.closed_at >= feed_cutoff)
              .order_by(ClosedTrade.closed_at.desc()).limit(30)):
        t_pnl = t.pnl or 0
        feed.append((_fts(t.closed_at), "exit",
                     f"{_hesc(t.tradingsymbol)} closed "
                     f"<b class='{'ok' if t_pnl >= 0 else 'bd'}'>"
                     f"{'+' if t_pnl >= 0 else '&minus;'}&#8377;{abs(t_pnl):,.0f}</b>"
                     f" <span class='fdim'>&middot; {_hesc(t.exit_reason)}</span>"))
    for e in (session.query(AppError).filter(AppError.occurred_at >= feed_cutoff)
              .order_by(AppError.occurred_at.desc()).limit(30)):
        feed.append((_fts(e.occurred_at), "err",
                     f"{_hesc(e.error_type)} <span class='fdim'>&middot; "
                     f"{_hesc((e.message or '')[:120])}</span>"))
    feed.sort(key=lambda it: it[0], reverse=True)
    feed = feed[:30]
    _FTAG = {"alert": "ALERT", "order": "ORDER", "gtt": "GTT", "exit": "EXIT", "err": "ERR"}
    return "".join(
        f"<li data-k='{kind}'>"
        f"<span class='ft'>{ts.astimezone(IST).strftime('%d %b %H:%M')}</span>"
        f"<span class='ftag ft-{kind}'>{_FTAG[kind]}</span>"
        f"<span class='fe'>{msg}</span></li>"
        for ts, kind, msg in feed
    ) or ("<li><span class='fe' style='text-align:center;color:#aaa'>"
          "No activity in the last 48h</span></li>")


def _performance_blocks(session: Session) -> tuple[str, str, list[list]]:
    """90-day tiles + Mon-Fri P&L heatmap + daily series — History tab.

    Returns (tiles_html, heatmap_html, perf_days) — perf_days seeds the
    client-side equity-curve chart (window.__perf).
    """
    perf_cutoff = datetime.now(IST) - timedelta(days=90)
    closed_rows = (
        session.query(ClosedTrade.closed_at, ClosedTrade.pnl)
        .filter(
            ClosedTrade.closed_at >= perf_cutoff,
            ClosedTrade.dry_run == False,  # noqa: E712
        )
        .order_by(ClosedTrade.closed_at.asc())
        .all()
    )
    daily_pnl: dict[str, float] = {}
    pnls: list[float] = []
    for ts, pnl in closed_rows:
        if pnl is None:
            continue
        day_key = _fts(ts).astimezone(IST).date().isoformat()
        daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + pnl
        pnls.append(pnl)
    n_trades = len(pnls)
    wins = [p for p in pnls if p > 0]
    gross_w = sum(wins)
    gross_l = abs(sum(p for p in pnls if p < 0))
    win_rate = len(wins) / n_trades * 100 if n_trades else None
    profit_factor = gross_w / gross_l if gross_l > 0 else None
    expectancy = sum(pnls) / n_trades if n_trades else None
    _cum = _peak = 0.0
    max_dd = 0.0
    for p in pnls:
        _cum += p
        _peak = max(_peak, _cum)
        max_dd = min(max_dd, _cum - _peak)
    perf_days = sorted((k, round(v)) for k, v in daily_pnl.items())

    def _tile(val: str, label: str, cls: str = "") -> str:
        return (f"<div class='tile'><div class='tv2 {cls}'>{val}</div>"
                f"<div class='tl2'>{label}</div></div>")

    tiles_html = (
        _tile(f"{win_rate:.0f}%" if win_rate is not None else "&mdash;", "win rate")
        + _tile(f"{profit_factor:.2f}" if profit_factor is not None else "&mdash;", "profit factor")
        + _tile((f"{'+' if expectancy > 0 else ('&minus;' if expectancy < 0 else '')}"
                 f"&#8377;{abs(expectancy):,.0f}") if expectancy is not None else "&mdash;",
                "expectancy / trade",
                "ok" if (expectancy or 0) > 0 else ("bd" if (expectancy or 0) < 0 else ""))
        + _tile(f"&minus;&#8377;{abs(max_dd):,.0f}" if max_dd < 0 else "&#8377;0",
                "max drawdown", "bd" if max_dd < 0 else "")
        + _tile(f"{n_trades}", "closed trades")
    )

    # P&L calendar: 6 ISO weeks × Mon–Fri, server-rendered cells
    today_d = datetime.now(IST).date()
    monday = today_d - timedelta(days=today_d.weekday())
    hm_weeks = [monday - timedelta(weeks=w) for w in range(5, -1, -1)]
    hm_max = max((abs(v) for v in daily_pnl.values()), default=0.0)
    hm_cells: list[str] = []
    for dow in range(5):
        for wk in hm_weeks:
            d = wk + timedelta(days=dow)
            v = daily_pnl.get(d.isoformat())
            if d > today_d or v is None:
                hm_cells.append(f"<div title='{d.strftime('%d %b')}'></div>")
            else:
                alpha = 0.2 + 0.8 * min(1.0, abs(v) / hm_max) if hm_max else 0.2
                colour = (f"rgba(52,199,89,{alpha:.2f})" if v > 0
                          else (f"rgba(255,59,48,{alpha:.2f})" if v < 0 else ""))
                style = f" style='background:{colour}'" if colour else ""
                sign = "+" if v > 0 else ("-" if v < 0 else "")
                hm_cells.append(
                    f"<div{style} title='{d.strftime('%d %b')} &middot; "
                    f"{sign}&#8377;{abs(v):,.0f}'></div>")
    hm_html = "<div class='hm'>" + "".join(hm_cells) + "</div>"
    return tiles_html, hm_html, perf_days


# The deploy-small-then-scale gate: a strategy earns size only after ~30 live
# trading days of positive evidence.
_SCORE_PROVEN_DAYS = 30


def _scorecard_html(session: Session) -> str:
    """Per-strategy stats over 90 days, paper vs live — History tab."""
    perf_cutoff = datetime.now(IST) - timedelta(days=90)

    def inr_s(v: float) -> str:
        sign = "+" if v > 0 else ("&minus;" if v < 0 else "")
        return f"{sign}&#8377;{abs(v):,.0f}"
    sc_rows = (
        session.query(
            ClosedTrade.strategy_id, ClosedTrade.dry_run,
            ClosedTrade.closed_at, ClosedTrade.pnl,
        )
        .filter(ClosedTrade.closed_at >= perf_cutoff)
        .order_by(ClosedTrade.closed_at.asc())
        .all()
    )
    _sc_groups: dict[tuple[str, bool], dict] = {}
    for _sid, _sdry, _sts, _spnl in sc_rows:
        if _spnl is None:
            continue
        _key = (_scorecard_group(_sid), bool(_sdry))
        g = _sc_groups.setdefault(_key, {"pnls": [], "days": set()})
        g["pnls"].append(_spnl)
        g["days"].add(_fts(_sts).astimezone(IST).date())

    def _sc_stats(g: dict) -> dict:
        _pnls = g["pnls"]
        _n = len(_pnls)
        _wins = sum(1 for p in _pnls if p > 0)
        _cum = _pk = 0.0
        _dd = 0.0
        for p in _pnls:
            _cum += p
            _pk = max(_pk, _cum)
            _dd = min(_dd, _cum - _pk)
        return {
            "n": _n,
            "win_rate": _wins / _n * 100 if _n else 0.0,
            "expectancy": sum(_pnls) / _n if _n else 0.0,
            "total": sum(_pnls),
            "max_dd": _dd,
            "days": len(g["days"]),
        }

    def _sc_name(sid: str) -> str:
        label = {
            "tv_webhook": "TV webhook",
            "voice": "voice",
            "voice_straddle": "voice straddle",
            "manual": "manual / other",
        }.get(sid, sid)
        if sid.startswith("sched_straddle_"):
            label = sid.removeprefix("sched_straddle_") + " straddle"
        elif sid.startswith("ws_"):
            label = sid.removeprefix("ws_").replace("_", " ") + " window"
        return label if len(label) <= 28 else label[:27] + "&hellip;"

    _sc_entries = sorted(
        ((k, _sc_stats(g)) for k, g in _sc_groups.items()),
        key=lambda kv: (kv[0][1], -kv[1]["total"]),  # live first, then total P&L desc
    )
    sc_trs: list[str] = []
    for (_sid, _sdry), st in _sc_entries:
        if _sdry:
            _badge = "<span class='scb scb-p'>&#128221; paper</span>"
        elif st["days"] >= _SCORE_PROVEN_DAYS and st["total"] > 0:
            _badge = f"<span class='scb scb-ok'>proven &middot; {st['days']}d</span>"
        else:
            _badge = f"<span class='scb scb-wn'>proving &middot; {st['days']}/{_SCORE_PROVEN_DAYS}d</span>"
        _exp_cls = "ok" if st["expectancy"] > 0 else ("bd" if st["expectancy"] < 0 else "")
        _tot_cls = "ok" if st["total"] > 0 else ("bd" if st["total"] < 0 else "")
        sc_trs.append(
            "<tr>"
            f"<td class='scn'>{_sc_name(_sid)}</td>"
            f"<td>{_badge}</td>"
            f"<td>{st['n']}</td>"
            f"<td>{st['win_rate']:.0f}%</td>"
            f"<td class='{_exp_cls}'>{inr_s(st['expectancy'])}</td>"
            f"<td class='{_tot_cls}'>{inr_s(st['total'])}</td>"
            f"<td class='bd'>{('&minus;&#8377;' + format(abs(st['max_dd']), ',.0f')) if st['max_dd'] < 0 else '&#8377;0'}</td>"
            "</tr>"
        )
    return (
        "<div class='sct-wrap'><table class='sct'>"
        "<tr><th>strategy</th><th></th><th>trades</th><th>win</th>"
        "<th>expect/tr</th><th>total P&amp;L</th><th>max DD</th></tr>"
        + "".join(sc_trs)
        + "</table></div>"
        if sc_trs else
        "<div style='color:#aaa;text-align:center;padding:12px 0'>"
        "No closed trades in the last 90 days</div>"
    )


def _today_snapshots(session: Session) -> list[list]:
    """Intraday P&L snapshots (today) seeding the Home hero sparkline."""
    _snap_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        [_fts(r.at).astimezone(IST).strftime("%H:%M"),
         round(r.realized + (r.open_mtm or 0.0))]
        for r in (session.query(PnlSnapshot)
                  .filter(PnlSnapshot.at >= _snap_start)
                  .order_by(PnlSnapshot.at.asc()).all())
    ]


def _book_seg(active_book: str) -> str:
    """Orders/GTTs switcher — both are reached from the More panel's link-tiles."""
    return (
        "<div style='margin-bottom:14px'><span class='seg'>"
        f"<a href='/orders' class='{'on' if active_book == 'orders' else ''}'>Orders</a>"
        f"<a href='/gtts' class='{'on' if active_book == 'gtts' else ''}'>GTTs</a>"
        "</span></div>"
    )


def _shell(active: str, content: str, settings: Settings, wide: bool = False, refresh: bool = False,
           live: bool = False) -> str:
    """Wrap page content in the shared topbar / status strip / bottom tab bar / CSS.

    refresh=True adds a 120s meta-refresh; live=True shows the updated-at
    stamp without the meta refresh (the page polls its own JSON instead).
    `active` is one of the 4 tabs (home/markets/settings/more). Home/Markets/
    Settings are swipeable panels *within* /control (see control_page); the
    tab bar links there as /control#<tab> so it still works — as a normal
    page load landing on the right panel — from every other page. More is a
    real panel too (link-tiles to Orders/GTTs/History/Alerts/Agents/Desk),
    so those pages (and any other admin/debug page) pass active="more".
    """
    tabs = [
        ("/control#home", "Home", "home",
         "<path d='M4 11.5 12 4l8 7.5'/><path d='M6 10v9h12v-9'/>"),
        ("/control#markets", "Markets", "markets",
         "<path d='M4 16l5-5 4 4 7-8'/><path d='M15 7h6v6'/>"),
        ("/control#settings", "Settings", "settings",
         "<path d='M4 7h10M18 7h2M4 17h2M8 17h12'/><circle cx='16' cy='7' r='2.3'/><circle cx='6' cy='17' r='2.3'/>"),
        ("/control#more", "More", "more",
         "<circle cx='5' cy='12' r='1.8'/><circle cx='12' cy='12' r='1.8'/><circle cx='19' cy='12' r='1.8'/>"),
    ]
    tabbar = "<nav class='tabbar' aria-label='Primary'><div class='tabbar-in'>" + "".join(
        f"<a href='{href}' class='tb-item{' on' if key == active else ''}' data-tab='{key}'>"
        f"<svg viewBox='0 0 24 24'>{svg}</svg><span>{lbl}</span></a>"
        for href, lbl, key, svg in tabs
    ) + "</div></nav>"
    stop_banner, strip_html = _status_strip(settings)
    wrap_cls = "wrap wrap-lg" if wide else "wrap wrap-sm"
    refresh_meta = "<meta http-equiv='refresh' content='120'>" if refresh else ""
    lut_html = (
        "<span id='lut' class='tb-lut'></span>"
        if (refresh or live) else "<span class='tb-lut'></span>"
    )
    lut_js = (
        "<script>window.addEventListener('load',function(){"
        "var e=document.getElementById('lut');"
        "if(e)e.textContent='Updated '+new Date().toLocaleTimeString();})"
        "</script>"
        if (refresh or live) else ""
    )
    return (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        "<title>ZeroBot</title>"
        + refresh_meta +
        "<link rel='manifest' href='/manifest.json'>"
        "<meta name='theme-color' content='#F5F5F7' media='(prefers-color-scheme: light)'>"
        "<meta name='theme-color' content='#0A0A0C' media='(prefers-color-scheme: dark)'>"
        "<meta name='apple-mobile-web-app-capable' content='yes'>"
        "<meta name='apple-mobile-web-app-status-bar-style' content='default'>"
        "<meta name='apple-mobile-web-app-title' content='ZeroBot'>"
        "<link rel='apple-touch-icon' href='/icon-192.png'>"
        "<style>" + _CSS + "</style>"
        "<script>if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js')</script>"
        "</head><body>"
        "<header class='topbar'><div class='tb-in'>"
        "<span class='wordmark'><i></i>ZeroBot</span>"
        + lut_html +
        "<a class='tb-out' href='/auth/logout'>Sign out</a>"
        "</div></header>"
        + tabbar +
        # tabbar renders before .wrap (though fixed-position, so this doesn't
        # move it visually) so its <a data-tab> elements already exist in the
        # DOM by the time an inline script inside `content` (_SWIPE_JS) runs
        # and queries for them — scripts execute as the parser reaches them,
        # and a script can't see elements that appear later in the HTML.
        "<div class='" + wrap_cls + "'>" + stop_banner + strip_html + content + "</div>"
        + lut_js +
        "</body></html>"
    )


@app.get("/dashboard")
async def dashboard(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
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
        content=_shell("more",
            filters
            + "<div class='card'><div class='ct'>Recent Alerts</div>"
            "<table><thead><tr><th>ID</th><th>Ticker</th><th>Action</th>"
            "<th>Time</th><th>Processed</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            settings,
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/positions")
async def positions(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
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
            settings,
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/history")
async def history(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    days: int = Query(default=1, ge=0),
) -> Response:
    q = session.query(ClosedTrade)
    if days > 0:
        q = q.filter(ClosedTrade.closed_at >= datetime.now(IST) - timedelta(days=days))
    rows = q.order_by(ClosedTrade.closed_at.desc()).limit(200).all()

    total_pnl = sum((r.pnl or 0) for r in rows if not r.dry_run)
    n_paper = sum(1 for r in rows if r.dry_run)
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
        + (f"<span style='font-size:.72em;color:#8492a6'>&#128221; {n_paper} paper excluded</span>" if n_paper else "")
        + f"<span style='font-size:.78em;color:#8492a6;margin-left:auto'>{len(rows)} trade(s)</span>"
        f"</div>"
    )

    rows_html = "".join(
        f"<tr><td>{r.id}</td><td style='font-weight:600'>{r.tradingsymbol}"
        f"{' &#128221;' if r.dry_run else ''}</td>"
        f"<td class='tr {'ok' if (r.pnl or 0) >= 0 else 'bd'}'>&#x20B9;{(r.pnl or 0):+.2f}</td>"
        f"<td>{r.exit_reason}</td>"
        f"<td>{r.closed_at.strftime('%m/%d %H:%M') if r.closed_at else '—'}</td></tr>"
        for r in rows
    )

    # ── performance review — moved here from /control so Home stays short ────
    tiles_html, hm_html, perf_days = _performance_blocks(session)
    performance_card = (
        "<div class='card'><div class='ct'>Performance &mdash; 90 days</div>"
        "<div class='tiles'>" + tiles_html + "</div>"
        "<div id='eq-chart' style='padding:6px 18px 4px'></div>"
        "<div class='hmwrap'><div class='hmlbl'>Daily P&amp;L &middot; last 6 weeks"
        " &middot; rows Mon&rarr;Fri</div>" + hm_html + "</div></div>"
    )
    scorecard_card = (
        "<div class='card'><div class='ct'>Strategies &mdash; 90 days"
        "<span style='margin-left:auto;text-transform:none;letter-spacing:0;"
        f"font-weight:400;color:var(--ink3)'>scale after {_SCORE_PROVEN_DAYS} live days</span></div>"
        + _scorecard_html(session) + "</div>"
    )
    activity_card = (
        "<div class='card'><div class='ct'>Activity &mdash; 48h</div>"
        "<div class='fchips'>"
        "<button class='fchip on' data-f='all'>All</button>"
        "<button class='fchip' data-f='alert'>Alerts</button>"
        "<button class='fchip' data-f='order'>Orders</button>"
        "<button class='fchip' data-f='gtt'>GTTs</button>"
        "<button class='fchip' data-f='exit'>Exits</button>"
        "<button class='fchip' data-f='err'>Errors</button>"
        "</div><ul class='feed' id='feed'>" + _activity_feed_html(session) + "</ul></div>"
    )
    live_js = (
        "<script>" + f"window.__perf={json.dumps({'days': perf_days})};" + "</script>"
        + _HISTORY_LIVE_JS
    )

    return Response(
        content=_shell("more",
            performance_card + scorecard_card + activity_card
            + filters + summary
            + "<div class='card'><div class='ct'>Trade History</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th><th class='tr'>PnL</th>"
            "<th>Reason</th><th>Closed</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>"
            + live_js,
            settings,
            wide=True,
            live=True,
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
        content=_shell("more",
            _book_seg("gtts")
            + toggle_bar
            + "<div class='card'><div class='ct'>GTT / Stop-Loss Orders</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th><th class='tr'>SL Trigger</th>"
            "<th class='tr'>Target</th><th class='tr'>Entry Price</th><th>DB Status</th>"
            "<th class='tc'>Kite GTT ID</th><th class='tc'>Kite Live</th><th>Placed At</th>"
            "</tr></thead><tbody>" + rows_html + "</tbody></table>"
            "<p style='font-size:.73em;color:#aaa;margin-top:8px'>"
            "Kite Live: <b>active</b>=SL live | <b>triggered</b>=fired | "
            "<b>N/A</b>=not found (triggered+cleaned up or GTT_FAILED)</p></div>",
            settings,
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


def _todays_schedule(settings: Settings) -> tuple[str, str]:
    """Render today's automated-job rail for /control.

    Returns (rail_html, next_label) where next_label describes the first
    upcoming enabled job ("NATURALGAS straddle ×1 in 41m") or "" if none left.
    """
    from app.window_straddle import WINDOW_STRADDLE_CFG

    now = datetime.now(IST)
    today_wd = now.weekday()

    items: list[tuple[int, str, str, bool, str]] = []

    def add(hhmm: str, label: str, enabled: bool = True, note: str = "") -> None:
        try:
            h, m = str(hhmm).split(":")
            t = int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return
        items.append((t, f"{int(h):02d}:{int(m):02d}", label, enabled, note))

    add(settings.KITE_AUTO_LOGIN_TIME, "Kite auto-login", settings.PYOTP_AUTO_LOGIN)
    add(f"{settings.SCHEDULER_HOUR_IST:02d}:{settings.SCHEDULER_MINUTE_IST:02d}", "Session check")
    add("08:30", "Instrument refresh")
    ws_on = state.is_window_straddle_enabled()
    for und, cfg in WINDOW_STRADDLE_CFG.items():
        for entry_hhmm, exit_hhmm, allowed_days in cfg["windows"]:
            if today_wd in allowed_days:
                add(entry_hhmm, f"{und} window straddle &times;{cfg['qty']}", ws_on)
                add(exit_hhmm, f"{und} window straddle exit", ws_on)
    add(settings.EXPIRY_DAY_SQUAREOFF_TIME, "Expiry-day squareoff", True, "expiry days only")
    add(settings.NSE_SQUAREOFF_TIME, "NSE EOD squareoff")
    if settings.STRADDLE_DEFENSE_PREHEDGE_TIME:
        add(settings.STRADDLE_DEFENSE_PREHEDGE_TIME, "Straddle-defense pre-hedge window",
            state.is_straddle_defense_enabled(settings.STRADDLE_DEFENSE_ENABLED), "reminder")
    sched_on = settings.SCHEDULED_STRADDLE_ENABLED
    add(settings.NG_STRADDLE_TIME,
        f"NATURALGAS straddle &times;{settings.NG_STRADDLE_QTY}", sched_on,
        f"ADX&lt;{settings.NG_STRADDLE_ADX_THRESHOLD:g} gate")
    add(settings.STRADDLE_SQUAREOFF_TIME, "Sched. straddle squareoff", sched_on)
    add(settings.MCX_SQUAREOFF_TIME, "MCX EOD squareoff")

    items.sort(key=lambda it: (it[0], it[2]))
    now_m = now.hour * 60 + now.minute

    rows: list[str] = []
    next_label = ""
    for t, hhmm, label, enabled, note in items:
        if not enabled:
            cls, status = "r-off", "off"
        elif t <= now_m:
            cls, status = "r-done", "&#x2713;"
        elif not next_label:
            cls, status = "r-next", "&#x25CF; next"
            dt_min = t - now_m
            plain = label.replace("&times;", "×")
            next_label = (f"{plain} in {dt_min // 60}h {dt_min % 60:02d}m"
                          if dt_min >= 60 else f"{plain} in {dt_min}m")
        else:
            cls, status = "", "&ndash;"
        note_html = f" <span style='color:#AEAEB2'>&middot; {note}</span>" if note else ""
        rows.append(
            f"<li class='{cls}' data-t='{t}' data-en='{1 if enabled else 0}'>"
            f"<span class='rt'>{hhmm}</span>"
            f"<span class='re'>{label}{note_html}</span>"
            f"<span class='rs'>{status}</span></li>"
        )
    weekend = (
        "<li><span class='rt'></span><span class='re' style='color:#C93400'>"
        "Weekend &mdash; markets closed; cron jobs idle</span><span class='rs'></span></li>"
        if today_wd >= 5 else ""
    )
    return "<ul class='rail'>" + weekend + "".join(rows) + "</ul>", next_label


@app.get("/control")
async def control_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
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

    # ── today's risk summary (scoped to the active paper/live mode) ───────────
    today_loss = _realised_loss_today(session, paper=paper)
    trades_today = _trades_today_count(session, paper=paper)
    consec = _consecutive_losses(session, paper=paper)
    open_pos = _open_position_count(session, paper=paper)
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
    ws_enabled  = state.is_window_straddle_enabled()
    ws_pill     = "pg" if ws_enabled else "pr"
    ws_label    = "ON" if ws_enabled else "OFF"
    ws_lbl      = "Disable" if ws_enabled else "Enable"
    ws_cls      = "ba" if ws_enabled else "bg2"
    hedge_enabled = state.is_ng_hedge_enabled(settings.NG_DELTA_HEDGE_ENABLED)
    hedge_pill    = "pg" if hedge_enabled else "pr"
    hedge_label   = "RUNNING" if hedge_enabled else "STOPPED"
    hedge_lbl     = "Stop"    if hedge_enabled else "Start"
    hedge_cls     = "br2"     if hedge_enabled else "bg2"
    crude_enabled = state.is_crude_hedge_enabled(settings.CRUDEOILM_HEDGE_ENABLED)
    crude_pill    = "pg" if crude_enabled else "pr"
    crude_label   = "RUNNING" if crude_enabled else "STOPPED"
    crude_lbl     = "Stop"    if crude_enabled else "Start"
    crude_cls     = "br2"     if crude_enabled else "bg2"
    pb_enabled    = state.is_partial_booking_enabled(settings.PARTIAL_BOOKING_ENABLED)
    pb_pill       = "pg" if pb_enabled else "pm"
    pb_label      = "ON" if pb_enabled else "OFF"
    pb_lbl        = "Disable" if pb_enabled else "Enable"
    pb_cls        = "ba" if pb_enabled else "bg2"
    ew_enabled    = state.is_entry_wings_enabled(settings.STRADDLE_ENTRY_WINGS_ENABLED)
    ew_pill       = "pg" if ew_enabled else "pm"
    ew_label      = "ON" if ew_enabled else "OFF"
    ew_lbl        = "Disable" if ew_enabled else "Enable"
    ew_cls        = "ba" if ew_enabled else "bg2"

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

    # ── today hero + schedule ─────────────────────────────────────────────────
    def inr(v: float) -> str:
        sign = "+" if v > 0 else ("&minus;" if v < 0 else "")
        return f"{sign}&#8377;{abs(v):,.0f}"

    realized = _realised_pnl_today(session, paper=paper)
    realized_cls = "ok" if realized > 0 else ("bd" if realized < 0 else "")
    loss_headroom = max(eff_max_loss - today_loss, 0)
    rail_html, next_label = _todays_schedule(settings)
    snaps = _today_snapshots(session)

    _ovr_keys = [
        "max_lots", "max_daily_loss", "sl_pct", "rr_ratio", "daily_profit_target",
        "sell_options_profit_pct", "entry_window_start", "entry_window_end",
        "no_entry_on_expiry_day", "max_trades_per_day", "max_open_positions",
        "capital_per_trade", "consecutive_losses_limit", "adx_threshold",
    ]
    n_ovr = sum(1 for k in _ovr_keys if overrides.get(k) is not None)

    sd_enabled = state.is_straddle_defense_enabled(settings.STRADDLE_DEFENSE_ENABLED)

    # ── straddle defense card ────────────────────────────────────────────────
    sd_trigger = settings.STRADDLE_DEFENSE_DRAWDOWN_TRIGGER
    sd_mode_cfg = state.get_straddle_defense_mode(settings.STRADDLE_DEFENSE_MODE)
    from app.straddle_defense import effective_mode as _sd_eff_mode
    sd_mode = _sd_eff_mode(settings)
    sd_mode_label = (f"{sd_mode_cfg} (unarmed &rarr; SEMI_AUTO)"
                     if sd_mode_cfg == "AUTO" and sd_mode != "AUTO" else sd_mode)
    # two-tap: first click flips the button to "Confirm?", second submits
    _tt = ("onclick=\"if(this.dataset.c!=='1'){this.dataset.c='1';var b=this;"
           "b.dataset.l=b.textContent;b.textContent='Confirm?';"
           "setTimeout(function(){b.dataset.c='';b.textContent=b.dataset.l;},4000);"
           "return false}\"")
    sd_rows: list = []
    if sd_enabled:
        try:
            from app.straddle_defense import current_status as _sd_status
            sd_rows = _sd_status(session, settings)
        except Exception as exc:
            log.warning("straddle-defense status unavailable: %s", exc)
    if not sd_enabled:
        sd_body = ("<div style='padding:13px 16px;font-size:.78em;color:#8E8E93'>"
                   "Monitor stopped &mdash; start it under Background Jobs.</div>")
    elif not sd_rows:
        sd_body = ("<div style='padding:13px 16px;font-size:.78em;color:#8E8E93'>"
                   "Watching &mdash; no short straddles tracked today yet.</div>")
    else:
        sd_parts = []
        for r in sd_rows:
            dd = r["drawdown"] or 0.0
            sd_pct = min(dd / sd_trigger * 100, 100) if sd_trigger else 0
            sd_bar, sd_vc = _bar(sd_pct)
            iv_txt = f"{r['iv_pct']:.1f}%" if r["iv_pct"] is not None else "&mdash;"
            arrow = " &#9650; rising" if r["iv_rising"] else ""
            upd = r["at"].astimezone(IST).strftime("%H:%M") if r["at"].tzinfo else r["at"].strftime("%H:%M")
            sd_parts.append(
                f"<div class='mr'><div class='ml'>{r['underlying']}</div>"
                f"<div class='mw'><div class='mb {sd_bar}' style='width:{sd_pct:.0f}%'></div></div>"
                f"<div class='mv {sd_vc}'>&minus;&#8377;{dd:,.0f}&thinsp;/&thinsp;&#8377;{sd_trigger:,.0f}</div></div>"
                f"<div style='padding:0 16px 9px;font-size:.7em;color:#8E8E93'>"
                f"MTM {inr(r['mtm']) if r['mtm'] is not None else '&mdash;'}"
                f" &middot; peak {inr(r['peak']) if r['peak'] is not None else '&mdash;'}"
                f" &middot; IV {iv_txt}{arrow}"
                f" &middot; alerts {r['alerts']}/{settings.STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY}"
                f" &middot; {upd}</div>"
            )
            h = r.get("hedge")
            if h is not None and h["status"] == "ACTIVE":
                cost = h["entry_cost"] if h["entry_cost"] is not None else h["est_cost"]
                sd_parts.append(
                    "<div class='mdr' style='background:var(--gain-soft)'>"
                    f"<div class='mdl'><span class='pill pg'>HEDGED</span>"
                    f"<span style='font-size:.72em;color:var(--ink2)'>&ensp;{h['ce_symbol']} + "
                    f"{h['pe_symbol']} qty {h['quantity']} &middot; cost &#8377;{cost:,.0f}"
                    f"{' &middot; paper' if h['dry_run'] else ''}</span></div>"
                    "<form method='post' action='/control/straddle-defense/unwind' style='margin:0'>"
                    f"<input type='hidden' name='action_id' value='{h['id']}'>"
                    f"<button class='btn br2' type='submit' {_tt}>Unwind</button></form></div>"
                )
            elif h is not None and h["status"] == "PROPOSED":
                exp = (h["expires_at"].astimezone(IST).strftime("%H:%M")
                       if h["expires_at"] is not None and h["expires_at"].tzinfo
                       else (h["expires_at"].strftime("%H:%M") if h["expires_at"] else "?"))
                sd_parts.append(
                    "<div class='mdr' style='background:var(--warn-soft)'>"
                    f"<div class='mdl'><span class='pill pa'>PROPOSED</span>"
                    f"<span style='font-size:.72em;color:var(--ink2)'>&ensp;BUY {h['ce_symbol']} + "
                    f"{h['pe_symbol']} qty {h['quantity']} &asymp; &#8377;{h['est_cost']:,.0f}"
                    f" &middot; {h['trigger']} &middot; expires {exp}</span></div>"
                    "<div style='display:flex;gap:6px'>"
                    "<form method='post' action='/control/straddle-defense/hedge/decision' style='margin:0'>"
                    f"<input type='hidden' name='action_id' value='{h['id']}'>"
                    f"<input type='hidden' name='token' value='{h['confirm_token']}'>"
                    "<input type='hidden' name='decision' value='approve'>"
                    f"<button class='btn bg2' type='submit' {_tt}>Approve</button></form>"
                    "<form method='post' action='/control/straddle-defense/hedge/decision' style='margin:0'>"
                    f"<input type='hidden' name='action_id' value='{h['id']}'>"
                    f"<input type='hidden' name='token' value='{h['confirm_token']}'>"
                    "<input type='hidden' name='decision' value='reject'>"
                    "<button class='btn br2' type='submit'>Reject</button></form>"
                    "</div></div>"
                )
            else:
                sd_parts.append(
                    "<div class='mdr'>"
                    "<div class='mdl'><span style='font-size:.72em;color:#8E8E93'>"
                    "No wings on &mdash; manual hedge builds a proposal to approve</span></div>"
                    "<form method='post' action='/control/straddle-defense/hedge' style='margin:0'>"
                    f"<input type='hidden' name='straddle_key' value='{r['key']}'>"
                    "<button class='btn bn' type='submit'>Hedge now</button></form></div>"
                )
        sd_body = "".join(sd_parts)
    sd_card = (
        "<div class='card'><div class='ct'>Straddle Defense"
        "<span style='margin-left:auto;text-transform:none;letter-spacing:0;color:#8E8E93'>"
        f"drawdown vs trigger &middot; mode {sd_mode_label}</span></div>"
        + sd_body + "</div>"
    )

    # ── volatility monitor card (chart drawn by _VOL_MONITOR_JS) ─────────────
    vol_options = "".join(
        f"<option value='{c}'>{n}</option>" for c, n in [
            ("NATURALGAS", "Natural Gas"), ("CRUDEOIL", "Crude Oil"),
            ("GOLD", "Gold"), ("SILVER", "Silver"),
            ("NIFTY", "Nifty"), ("BANKNIFTY", "Bank Nifty"),
        ]
    )
    vol_card = (
        "<div class='card'><div class='ct'>Volatility"
        "<span id='vm-msg' style='margin-left:auto;text-transform:none;"
        "letter-spacing:0;color:var(--ink3)'></span></div>"
        "<div class='vm-head'>"
        "<div class='vm-controls'>"
        "<span class='selw'><select id='vm-scrip' aria-label='Instrument'>"
        + vol_options +
        "</select></span>"
        "<span class='seg' id='vm-range' role='group' aria-label='Range'>"
        "<button data-d='1'>1D</button><button data-d='3'>3D</button>"
        "<button data-d='7' class='on'>7D</button><button data-d='99'>All</button>"
        "</span></div>"
        "<div class='vm-hero'><div class='vm-now' id='vm-now'>&mdash;</div>"
        "<span class='vm-state vm-std' id='vm-state'>loading&hellip;</span></div>"
        "</div>"
        "<div class='vm-chartwrap'><canvas id='vm-chart'></canvas>"
        "<div class='vm-tip' id='vm-tip'></div></div>"
        "<div class='vm-foot'>"
        "<span>24h ago <b id='vm-open'>&mdash;</b></span>"
        "<span>Range <b id='vm-rangev'>&mdash;</b></span>"
        "<span>Realised vol <b id='vm-rv'>&mdash;</b></span>"
        "<span>Premium edge <b id='vm-vrp'>&mdash;</b></span>"
        "<span class='vm-legend'>"
        "<span><i style='background:var(--loss-b)'></i>rising &mdash; hurts short vol</span>"
        "<span><i style='background:var(--gain-b)'></i>falling &mdash; theta wins</span>"
        "</span></div></div>"
    )

    # ── Home panel — act now: hero, risk meters, positions, defense, trade ────
    home_panel = (
        "<section class='panel' id='home'>"
        # today hero — realised is server-rendered; MTM/theta filled by JS
        + "<div class='card'><div class='ct'>Today</div>"
        + f"<div class='hero-pnl {realized_cls}' id='hero-net'>{inr(realized)}</div>"
        + "<div class='hero-sub'>"
        + f"<span>Realized <b id='hero-real' class='{realized_cls}'>{inr(realized)}</b></span>"
        + "<span>Open MTM <b id='hero-mtm'>&mdash;</b></span>"
        + "<span>&Theta;/day <b id='hero-theta'>&mdash;</b></span>"
        + f"<span>Loss budget left <b>&#8377;{loss_headroom:,.0f}</b></span>"
        + "</div>"
        + "<div id='day-spark' style='padding:0 18px 12px'></div>"
        + "</div>"

        # risk summary
        + "<div class='card'><div class='ct'>Today's Risk Summary</div>"
        + f"<div class='mr'><div class='ml'>Daily loss</div>"
        + f"<div class='mw'><div class='mb {loss_bar}' id='m-loss-b' style='width:{loss_pct_c:.0f}%'></div></div>"
        + f"<div class='mv {loss_vc}' id='m-loss-v'>&#x20B9;{today_loss:.0f}&thinsp;/&thinsp;&#x20B9;{eff_max_loss:.0f}</div></div>"
        + f"<div class='mr'><div class='ml'>Trades today</div>"
        + f"<div class='mw'><div class='mb {trades_bar}' id='m-trades-b' style='width:{min(trades_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {trades_vc}' id='m-trades-v'>{trades_today}&thinsp;/&thinsp;{eff_max_trades}</div></div>"
        + f"<div class='mr'><div class='ml'>Open positions</div>"
        + f"<div class='mw'><div class='mb {pos_bar}' id='m-pos-b' style='width:{min(pos_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {pos_vc}' id='m-pos-v'>{open_pos}&thinsp;/&thinsp;{eff_max_positions}</div></div>"
        + f"<div class='mr'><div class='ml'>Consec. losses</div>"
        + f"<div class='mw'><div class='mb {consec_bar}' id='m-consec-b' style='width:{min(consec_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {consec_vc}' id='m-consec-v'>{consec}&thinsp;/&thinsp;{eff_consec_limit}</div></div>"
        + "</div>"

        # open positions — filled by _CONTROL_LIVE_JS from /commodity-agents/portfolio-greeks
        + "<div class='card'><div class='ct'>Open Positions &mdash; Live Greeks</div>"
        + "<div id='pos-wrap' style='overflow-x:auto'>"
        + "<div id='pos-msg' style='padding:13px 18px;font-size:.78em;color:var(--ink3)'>"
        + "Loading live positions&hellip;</div></div></div>"

        + sd_card
        + _QUICK_TRADE_PANEL
        + "</section>"
    )

    # ── Markets panel — volatility + commodity intelligence ──────────────────
    markets_panel = (
        "<section class='panel' id='markets'>"
        + vol_card
        # commodity intelligence — filled by _CONTROL_LIVE_JS
        + "<div class='card'><div class='ct'>Commodity Intelligence"
        + "<span id='ca-msg' style='margin-left:auto;text-transform:none;letter-spacing:0;"
        + "color:var(--ink2)'></span></div>"
        + "<div class='cagrid' id='ca-grid'>"
        + "<div style='font-size:.78em;color:var(--ink3)'>Loading commodity data&hellip;</div>"
        + "</div></div>"
        + "</section>"
    )

    # ── Settings panel — configuration, checked occasionally ─────────────────
    settings_panel = (
        "<section class='panel' id='settings'>"
        # today's schedule
        + "<div class='card'><div class='ct'>Today's Schedule"
        + f"<span id='next-job' style='margin-left:auto;text-transform:none;letter-spacing:0;"
        + f"color:var(--accent)'>{('next: ' + next_label) if next_label else ''}</span>"
        + "</div>" + rail_html + "</div>"

        # mode / switches
        + "<div class='card'><div class='ct'>Mode</div>"
        + f"<div class='mdr'><div class='mdl'>Trade Mode&ensp;<span class='pill {mode_pill}'>{mode_display}</span>"
        + ("<span style='font-size:.72em;color:var(--ink3)'>&ensp;ranging: BUY->SELL CE&ensp;SELL->SELL PE&ensp;|&ensp;trending: falls back to SELL_OPTIONS</span>" if trade_mode == "RANGE_SELL" else "")
        + ("<span style='font-size:.72em;color:var(--ink3)'>&ensp;BUY->SELL PE&ensp;SELL->SELL CE</span>" if trade_mode == "SELL_OPTIONS" else "")
        + ("<span style='font-size:.72em;color:var(--ink3)'>&ensp;BUY->BUY CE&ensp;SELL->BUY PE</span>" if trade_mode == "BUY_OPTIONS" else "")
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
        + f"<div class='mdr'><div class='mdl'>Window Straddle&ensp;<span class='pill {ws_pill}'>{ws_label}</span><span style='font-size:.70em;color:var(--ink3)'>&ensp;NIFTY&times;2 BNF&times;1 CO&times;5 NG&times;2</span></div>"
        + "<form method='post' action='/control/window-straddle/toggle' style='margin:0'>"
        + f"<button class='btn {ws_cls}' type='submit'>{ws_lbl}</button></form></div>"
        + "<div style='margin-top:10px'><form method='post' action='/control/emergency-stop/toggle'>"
        + f"<button class='btn bfull {estop_cls}' type='submit'>{estop_lbl}</button>"
        + "</form></div></div>"

        # defined-risk controls — deploy-small-then-scale features 3 & 4
        + "<div class='card'><div class='ct'>Risk Controls</div>"
        + f"<div class='mdr'><div class='mdl'>Partial Profit Booking&ensp;<span class='pill {pb_pill}'>{pb_label}</span>"
        + f"<span style='font-size:.70em;color:var(--ink3)'>&ensp;books {settings.PARTIAL_BOOK_QTY_PCT*100:.0f}% of qty at "
        + f"{settings.PARTIAL_BOOK_TRIGGER_PCT*100:.0f}% to target, remainder to breakeven</span></div>"
        + "<form method='post' action='/control/partial-booking/toggle' style='margin:0'>"
        + f"<button class='btn {pb_cls}' type='submit'>{pb_lbl}</button></form></div>"
        + f"<div class='mdr'><div class='mdl'>Defined-Risk Wings&ensp;<span class='pill {ew_pill}'>{ew_label}</span>"
        + f"<span style='font-size:.70em;color:var(--ink3)'>&ensp;buys wings {settings.STRADDLE_ENTRY_WING_STEPS} strikes "
        + f"OTM before every new short straddle{' (required)' if settings.STRADDLE_ENTRY_WINGS_REQUIRED else ''}</span></div>"
        + "<form method='post' action='/control/entry-wings/toggle' style='margin:0'>"
        + f"<button class='btn {ew_cls}' type='submit'>{ew_lbl}</button></form></div>"
        + "</div>"

        # kite session
        + "<div class='card'><div class='ct'>Kite Session</div>"
        + f"<div class='mdr'><div class='mdl'>Status&ensp;<span class='pill {sess_pill}'>{sess_label}</span>"
        + f"<span style='font-size:.72em;color:var(--ink3)'>&ensp;{checked_str}</span></div>"
        + "<a href='/kite/login' class='btn bn' style='text-decoration:none'>Re-login</a></div>"
        + "</div>"

        # background jobs
        + "<div class='card'><div class='ct'>Background Jobs</div>"
        + f"<div class='mdr'><div class='mdl'>NG Delta Hedge&ensp;<span class='pill {hedge_pill}'>{hedge_label}</span>"
        + "<span style='font-size:.70em;color:var(--ink3)'>&ensp;every 2 min (odd) &middot; incl. half-exit, BNF SL, straddle ladder</span></div>"
        + "<form method='post' action='/control/ng-hedge/toggle' style='margin:0'>"
        + f"<button class='btn {hedge_cls}' type='submit'>{hedge_lbl}</button></form></div>"
        + f"<div class='mdr'><div class='mdl'>CRUDEOILM Delta Hedge&ensp;<span class='pill {crude_pill}'>{crude_label}</span>"
        + "<span style='font-size:.70em;color:var(--ink3)'>&ensp;every 5 min (:02) &middot; band in barrels</span></div>"
        + "<form method='post' action='/control/crude-hedge/toggle' style='margin:0'>"
        + f"<button class='btn {crude_cls}' type='submit'>{crude_lbl}</button></form></div>"
        + f"<div class='mdr'><div class='mdl'>Straddle Defense&ensp;<span class='pill {'pg' if sd_enabled else 'pr'}'>{'RUNNING' if sd_enabled else 'STOPPED'}</span>"
        + f"<span class='pill {'pa' if sd_mode != 'ALERT' else 'pm'}'>{sd_mode}</span>"
        + "<span style='font-size:.70em;color:var(--ink3)'>&ensp;1-min monitor &middot; drawdown+IV trigger &middot; wing hedging</span></div>"
        + "<div style='display:flex;gap:6px'>"
        + "<form method='post' action='/control/straddle-defense/mode' style='margin:0'>"
        + "<button class='btn bn' type='submit'>Mode</button></form>"
        + "<form method='post' action='/control/straddle-defense/toggle' style='margin:0'>"
        + f"<button class='btn {'br2' if sd_enabled else 'bg2'}' type='submit'>{'Stop' if sd_enabled else 'Start'}</button></form></div></div>"
        + "</div>"

        # risk params — collapsible drawer; summary shows effective values
        + "<details class='cfgd card'><summary>"
        + "<div class='ct'>Risk Parameters"
        + (f"<span class='pill pa' style='margin-left:8px'>{n_ovr} overridden</span>" if n_ovr else "")
        + "</div>"
        + "<div class='cfg-sum'>"
        + f"lots <b>{eff_max_lots}</b> &middot; loss cap <b>&#8377;{eff_max_loss:,.0f}</b>"
        + f" &middot; SL <b>{eff_sl_pct*100:g}%</b> &middot; R:R <b>{eff_rr:.1f}&times;</b>"
        + f" &middot; capital <b>&#8377;{eff_capital:,.0f}</b>"
        + f" &middot; window <b>{eff_entry_start}&ndash;{eff_entry_end}</b>"
        + " &middot; tap to edit</div>"
        + "</summary>"
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
        + "<div style='display:flex;gap:8px;padding:12px 18px 16px'>"
        + "<button class='btn bp' type='submit' style='flex:1'>Apply</button>"
        + "<button class='btn bm' type='submit' name='reset' value='1' style='flex:1'>Reset to defaults</button>"
        + "</div></form></details>"
        + "</section>"
    )

    # ── More panel — everything rarely used, one hop away ────────────────────
    _more_links = [
        ("/orders", "Orders", "Placed trades, fills, live P&amp;L per order"),
        ("/gtts", "GTTs", "Live stop-loss / target OCO orders"),
        ("/history", "History", "Closed trades, performance, scorecard"),
        ("/dashboard", "Alerts", "Raw TradingView webhook log"),
        ("/commodity-agents/dashboard", "Agents", "Commodity AI recommendations"),
        ("/commodity-agents/desk", "Desk", "Commodity agents trading desk"),
    ]
    more_panel = (
        "<section class='panel' id='more'>"
        + "<div class='morelinks'>" + "".join(
            f"<a class='mltile' href='{href}'><b>{label}</b><span>{desc}</span></a>"
            for href, label, desc in _more_links
        ) + "</div>"
        + "</section>"
    )

    swipe_html = (
        "<div class='swipe' id='swipe'>"
        + home_panel + markets_panel + settings_panel + more_panel
        + "</div>"
    )

    body = (
        swipe_html
        # seed the live script, then load it
        + "<script>"
        + f"window.__realized={realized:.0f};"
        + f"window.__estop={'true' if estop else 'false'};"
        + f"window.__paper={'true' if paper else 'false'};"
        + f"window.__mode={json.dumps(trade_mode)};"
        + f"window.__snaps={json.dumps(snaps)};"
        + "</script>"
        + _CONTROL_LIVE_JS
        + _VOL_MONITOR_JS
        + _SWIPE_JS
    )
    return Response(content=_shell("home", body, settings, live=True), media_type="text/html")


@app.get("/api/control/summary")
async def control_summary(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> dict:
    """Live numbers for /control's 30s poll — meters, hero, session, next job.

    The page reloads itself when emergency_stop / paper / trade_mode change,
    so those are included even though the poll doesn't patch them directly.
    """
    try:
        token_info = get_session_manager().get_token_info()
        sess_valid = bool(token_info["is_valid"])
    except Exception:
        sess_valid = False
    _, next_label = _todays_schedule(settings)
    _paper_now = _dry_run(settings)
    return {
        "realized": _realised_pnl_today(session, paper=_paper_now),
        "today_loss": _realised_loss_today(session, paper=_paper_now),
        "max_loss": state.get_max_daily_loss(settings.MAX_DAILY_LOSS_ABS),
        "trades_today": _trades_today_count(session, paper=_paper_now),
        "max_trades": state.get_max_trades_per_day(settings.MAX_TRADES_PER_DAY),
        "open_positions": _open_position_count(session, paper=_paper_now),
        "max_positions": state.get_max_open_positions(settings.MAX_OPEN_POSITIONS),
        "consec_losses": _consecutive_losses(session),
        "consec_limit": state.get_consecutive_losses_limit(settings.CONSECUTIVE_LOSSES_LIMIT),
        "paper": _dry_run(settings),
        "emergency_stop": state.is_emergency_stop(),
        "trade_mode": state.get_trade_mode(),
        "session_valid": sess_valid,
        "next_label": next_label,
    }


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


@app.post("/control/window-straddle/toggle")
async def toggle_window_straddle(_: None = Depends(_auth_guard)) -> Response:
    new_val = state.toggle_window_straddle()
    log.info("Window straddle toggled to %s", new_val)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/partial-booking/toggle")
async def toggle_partial_booking(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    new_val = state.toggle_partial_booking_enabled(settings.PARTIAL_BOOKING_ENABLED)
    log.info("Partial profit booking toggled to %s", new_val)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/entry-wings/toggle")
async def toggle_entry_wings(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    new_val = state.toggle_entry_wings_enabled(settings.STRADDLE_ENTRY_WINGS_ENABLED)
    log.info("Straddle entry wings toggled to %s", new_val)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/ng-hedge/toggle")
async def toggle_ng_hedge(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    from app.commodity_agents.notify import send_telegram

    new_val = state.toggle_ng_hedge_enabled(settings.NG_DELTA_HEDGE_ENABLED)
    log.warning("NG delta-hedge toggled to %s via /control", new_val)
    try:
        send_telegram(
            settings,
            f"⚡ NG Delta Hedge {'STARTED' if new_val else 'STOPPED'} via /control",
        )
    except Exception as exc:
        log.warning("NG delta-hedge toggle: telegram notify failed: %s", exc)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/crude-hedge/toggle")
async def toggle_crude_hedge(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    """Start/stop CRUDEOILM delta hedging.

    Runs in the same process as the scheduler, so the in-memory override takes
    effect on the very next due tick (:02, :07, ...) with no restart — and it
    persists to data/state_overrides.json so it survives one.
    """
    from app.commodity_agents.notify import send_telegram

    new_val = state.toggle_crude_hedge_enabled(settings.CRUDEOILM_HEDGE_ENABLED)
    log.warning("CRUDEOILM delta-hedge toggled to %s via /control", new_val)
    try:
        send_telegram(
            settings,
            f"⚡ CRUDEOILM Delta Hedge {'STARTED' if new_val else 'STOPPED'} via /control",
        )
    except Exception as exc:
        log.warning("CRUDEOILM delta-hedge toggle: telegram notify failed: %s", exc)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/straddle-defense/toggle")
async def toggle_straddle_defense(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    from app.commodity_agents.notify import send_telegram

    new_val = state.toggle_straddle_defense_enabled(settings.STRADDLE_DEFENSE_ENABLED)
    log.warning("straddle-defense monitor toggled to %s via /control", new_val)
    try:
        from app.straddle_defense import effective_mode as _sd_eff
        send_telegram(
            settings,
            f"\U0001f6e1 Straddle Defense monitor {'STARTED' if new_val else 'STOPPED'} via /control "
            f"(mode {_sd_eff(settings)})",
        )
    except Exception as exc:
        log.warning("straddle-defense toggle: telegram notify failed: %s", exc)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/straddle-defense/mode")
async def cycle_straddle_defense_mode(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    from app.commodity_agents.notify import send_telegram
    from app.straddle_defense import effective_mode as _sd_eff

    new_mode = state.cycle_straddle_defense_mode(settings.STRADDLE_DEFENSE_MODE)
    eff = _sd_eff(settings)
    log.warning("straddle-defense mode cycled to %s (effective %s) via /control", new_mode, eff)
    try:
        note = (" — AUTO is unarmed (STRADDLE_DEFENSE_AUTO_EXECUTE=false), acting as SEMI_AUTO"
                if new_mode == "AUTO" and eff != "AUTO" else "")
        send_telegram(settings, f"\U0001f6e1 Straddle Defense mode: {new_mode}{note}")
    except Exception as exc:
        log.warning("straddle-defense mode: telegram notify failed: %s", exc)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/straddle-defense/hedge")
async def straddle_defense_manual_hedge(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    db: Session = Depends(get_db_session),
    straddle_key: str = Form(...),
) -> Response:
    """Build a wing-hedge proposal for the straddle; the user then approves it
    on the card (single execution path for manual and automatic hedges)."""
    from app.straddle_defense import propose_hedge

    try:
        action = propose_hedge(db, settings, straddle_key, "manual",
                               datetime.now(IST), "MANUAL")
        db.commit()
        if action is None:
            log.warning("manual hedge for %s: no proposal created (see logs)", straddle_key)
    except Exception as exc:
        db.rollback()
        log.error("manual hedge for %s failed: %s", straddle_key, exc)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/straddle-defense/hedge/decision")
async def straddle_defense_hedge_decision(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    db: Session = Depends(get_db_session),
    action_id: int = Form(...),
    token: str = Form(...),
    decision: str = Form(...),
) -> Response:
    from app.straddle_defense import decide_hedge

    try:
        ok, msg = decide_hedge(db, settings, action_id, token,
                               decision == "approve", datetime.now(IST))
        db.commit()
        log.warning("hedge decision #%d %s: %s (%s)", action_id, decision, ok, msg)
    except Exception as exc:
        db.rollback()
        log.error("hedge decision #%d failed: %s", action_id, exc)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/straddle-defense/unwind")
async def straddle_defense_unwind(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    db: Session = Depends(get_db_session),
    action_id: int = Form(...),
) -> Response:
    from app.storage import HedgeAction
    from app.straddle_defense import unwind_hedge

    try:
        action = db.get(HedgeAction, action_id)
        if action is not None:
            unwind_hedge(db, settings, action, datetime.now(IST), reason="manual")
        db.commit()
    except Exception as exc:
        db.rollback()
        log.error("hedge unwind #%d failed: %s", action_id, exc)
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
    settings: Settings = Depends(get_current_settings),
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
        content=_shell("more",
            _book_seg("orders")
            + filters + summary
            + "<div class='card'><div class='ct'>Orders</div>"
            "<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th class='tr'>Lots</th>"
            "<th class='tr'>Entry</th><th class='tr'>SL</th><th class='tr'>Target</th>"
            "<th class='tr'>P&amp;L</th><th>Status</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            settings,
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
