from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker

from app import state
from app.config import IST, get_settings
from app.kite_session import get_session_manager

log = logging.getLogger(__name__)

_last_checked_at: datetime | None = None


def daily_session_check(now: datetime | None = None) -> None:
    """Validate the Kite access token; update SESSION_INVALID flag. Inject now for testability."""
    global _last_checked_at
    if now is None:
        now = datetime.now(IST)
    _last_checked_at = now

    try:
        kite = get_session_manager().get_kite()
        kite.profile()
        state.set_session_invalid(False)
        log.info("[scheduler] session OK for %s", now.date())
    except Exception as exc:
        state.set_session_invalid(True)
        log.warning("[scheduler] session invalid — manual login required: %s", exc)


def get_last_checked_at() -> datetime | None:
    return _last_checked_at


def refresh_instruments_job(session_factory: Any) -> None:
    """Download Kite instruments CSV and reload the instruments table."""
    from app.symbol_mapper import refresh_instruments
    from app.storage import init_db
    try:
        with session_factory() as session:
            n = refresh_instruments(session)
            log.info("[scheduler] instruments refreshed: %d rows", n)
    except Exception as exc:
        log.error("[scheduler] instrument refresh failed: %s", exc)


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def eod_squareoff_job(
    session_factory: Any,
    exchanges: list[str],
    reason: str,
    dry_run: bool = False,
    symbols_filter: set[str] | None = None,
) -> None:
    """Close open positions for the given exchanges.

    symbols_filter: if provided, only close positions whose tradingsymbol is in the set
    (used by expiry_day_squareoff_job to close only today's expiring contracts).
    """
    from app.storage import ClosedTrade, Gtt, Instrument, Order, Position
    from app.orders import cancel_gtt, square_off

    if dry_run:
        log.info("[scheduler] %s squareoff DRY_RUN — exchanges=%s", reason, exchanges)
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[scheduler] %s squareoff: cannot get kite client: %s", reason, exc)
        return

    now = datetime.now(IST)
    with session_factory() as session:
        q = (
            session.query(Position)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.exchange.in_(exchanges),
                ClosedTrade.id == None,  # noqa: E711
            )
        )
        if symbols_filter:
            q = q.filter(Position.tradingsymbol.in_(symbols_filter))
        open_positions = q.all()
        if not open_positions:
            log.info("[scheduler] %s squareoff: no open positions for %s", reason, exchanges)
            return

        for position in open_positions:
            try:
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
                            "[scheduler] %s: cancel GTT %d failed: %s",
                            reason, gtt.kite_gtt_id, exc,
                        )
                    gtt.status = "CANCELLED"
                    gtt.updated_at = now

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
                        "[scheduler] %s: instrument not found for %s",
                        reason, position.tradingsymbol,
                    )
                    continue

                product = entry_order.product if entry_order else "NRML"
                entry_side = (
                    entry_order.transaction_type if entry_order else "BUY"
                )

                sq_id = square_off(
                    kite, instrument, position.quantity,
                    product=product, entry_side=entry_side,
                )
                ct = ClosedTrade(
                    position_id=position.id,
                    exchange=position.exchange,
                    tradingsymbol=position.tradingsymbol,
                    entry_premium=position.entry_premium,
                    exit_premium=0.0,
                    pnl=0.0,
                    exit_reason=reason,
                    opened_at=position.opened_at,
                    closed_at=now,
                )
                session.add(ct)
                log.info(
                    "[scheduler] %s squareoff: %s qty=%d sq_order=%s",
                    reason, position.tradingsymbol, position.quantity, sq_id,
                )
            except Exception as exc:
                log.error(
                    "[scheduler] %s squareoff failed for %s: %s",
                    reason, position.tradingsymbol, exc, exc_info=True,
                )

        session.commit()


def expiry_day_squareoff_job(session_factory: Any, dry_run: bool = False) -> None:
    """Close NFO options positions that expire today (runs at EXPIRY_DAY_SQUAREOFF_TIME)."""
    from app.storage import Instrument, Position, ClosedTrade
    from datetime import date

    now = datetime.now(IST)
    today = now.date()

    with session_factory() as session:
        expiring_symbols = {
            row[0]
            for row in session.query(Instrument.tradingsymbol).filter(
                Instrument.exchange.in_(["NSE", "NFO"]),
                Instrument.expiry == today,
                Instrument.instrument_type.in_(["CE", "PE"]),
            )
        }
        if not expiring_symbols:
            return

        symbols_with_open_positions = (
            session.query(Position.tradingsymbol)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol.in_(expiring_symbols),
                ClosedTrade.id == None,  # noqa: E711
            )
            .distinct()
            .all()
        )
        if not symbols_with_open_positions:
            return

        log.info(
            "[scheduler] expiry-day squareoff: %d positions expiring today",
            len(symbols_with_open_positions),
        )

    eod_squareoff_job(
        session_factory=session_factory,
        exchanges=["NSE", "NFO"],
        reason="EXPIRY_DAY",
        dry_run=dry_run,
        symbols_filter=expiring_symbols,
    )


def make_scheduler(settings: Any = None, session_factory: Any = None) -> BackgroundScheduler:
    if settings is None:
        settings = get_settings()
    scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Kolkata"))
    scheduler.add_job(
        daily_session_check,
        trigger="cron",
        hour=settings.SCHEDULER_HOUR_IST,
        minute=settings.SCHEDULER_MINUTE_IST,
        id="daily_session_check",
    )
    if session_factory is not None:
        # Refresh instruments daily at 8:30 AM IST, before market open.
        scheduler.add_job(
            refresh_instruments_job,
            trigger="cron",
            hour=8,
            minute=30,
            args=[session_factory],
            id="daily_instrument_refresh",
        )
        settings_obj = settings  # local alias
        nse_h, nse_m = _parse_hhmm(settings_obj.NSE_SQUAREOFF_TIME)
        scheduler.add_job(
            eod_squareoff_job,
            trigger="cron",
            hour=nse_h,
            minute=nse_m,
            kwargs={
                "session_factory": session_factory,
                "exchanges": ["NSE", "NFO"],
                "reason": "EOD_NSE",
                "dry_run": settings_obj.DRY_RUN,
            },
            id="nse_eod_squareoff",
        )
        mcx_h, mcx_m = _parse_hhmm(settings_obj.MCX_SQUAREOFF_TIME)
        scheduler.add_job(
            eod_squareoff_job,
            trigger="cron",
            hour=mcx_h,
            minute=mcx_m,
            kwargs={
                "session_factory": session_factory,
                "exchanges": ["MCX", "MCX-OPT"],
                "reason": "EOD_MCX",
                "dry_run": settings_obj.DRY_RUN,
            },
            id="mcx_eod_squareoff",
        )
        exp_h, exp_m = _parse_hhmm(settings_obj.EXPIRY_DAY_SQUAREOFF_TIME)
        scheduler.add_job(
            expiry_day_squareoff_job,
            trigger="cron",
            hour=exp_h,
            minute=exp_m,
            kwargs={
                "session_factory": session_factory,
                "dry_run": settings_obj.DRY_RUN,
            },
            id="expiry_day_squareoff",
        )
    return scheduler
