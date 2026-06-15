from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests.exceptions

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker

from app import state
from app.config import IST, get_settings
from app.kite_session import get_session_manager

log = logging.getLogger(__name__)

_precise_timers: list["_PreciseTimer"] = []


class _PreciseTimer:
    """Dedicated thread that fires a callback at a fixed HH:MM IST daily.

    Avoids APScheduler's thread-pool scheduling jitter under CPU throttling.
    Wakes 10 s before target to ensure the thread is warm, then busy-waits
    the final window so the job fires within ~100 ms of its target time.
    """

    def __init__(self, name: str, hour: int, minute: int, callback: Callable, *args: Any, **kwargs: Any) -> None:
        self._name = name
        self._hour = hour
        self._minute = minute
        self._callback = callback
        self._args = args
        self._kwargs = kwargs
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"precise-timer-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("[PreciseTimer] %s scheduled daily at %02d:%02d IST", self._name, self._hour, self._minute)

    def stop(self) -> None:
        self._stop_event.set()

    def _next_fire(self) -> datetime:
        now = datetime.now(IST)
        target = now.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _run(self) -> None:
        while not self._stop_event.is_set():
            target = self._next_fire()
            # Sleep until 10 s before target — coarse sleep keeps thread in RAM
            coarse_wait = (target - datetime.now(IST)).total_seconds() - 10
            if coarse_wait > 0:
                self._stop_event.wait(timeout=coarse_wait)
            if self._stop_event.is_set():
                break
            # Tight busy-wait for final 10 s so we fire within ~100 ms
            while datetime.now(IST) < target:
                time.sleep(0.05)
            if self._stop_event.is_set():
                break
            log.info("[PreciseTimer] %s firing at %s", self._name, datetime.now(IST).strftime("%H:%M:%S"))
            try:
                self._callback(*self._args, **self._kwargs)
            except Exception as exc:
                log.error("[PreciseTimer] %s callback failed: %s", self._name, exc, exc_info=True)

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
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    ) as exc:
        log.warning("[scheduler] session check skipped — transient network error (session state unchanged): %s", exc)
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


def auto_login_job(
    watcher_restart_fn: Callable[[str, str], None] | None = None,
) -> None:
    """Run headless Kite login and restart the OrderWatcher with the fresh token."""
    from app.auto_login import auto_kite_login, KiteAutoLoginError
    settings = get_settings()

    if not settings.PYOTP_AUTO_LOGIN:
        log.debug("[scheduler] auto-login skipped (PYOTP_AUTO_LOGIN=false)")
        return

    try:
        auto_kite_login(settings, on_success=watcher_restart_fn)
        # Immediately validate the fresh token so SESSION_INVALID is cleared
        # and the 08:00 session check reflects the actual login time.
        daily_session_check()
    except KiteAutoLoginError as exc:
        state.set_session_invalid(True)
        log.error("[scheduler] auto-login failed: %s", exc)
    except Exception as exc:
        state.set_session_invalid(True)
        log.error("[scheduler] auto-login unexpected error: %s", exc, exc_info=True)


def make_scheduler(
    settings: Any = None,
    session_factory: Any = None,
    watcher_restart_fn: Callable[[str, str], None] | None = None,
) -> BackgroundScheduler:
    if settings is None:
        settings = get_settings()
    scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Kolkata"))

    if settings.PYOTP_AUTO_LOGIN:
        al_h, al_m = _parse_hhmm(settings.KITE_AUTO_LOGIN_TIME)
        scheduler.add_job(
            auto_login_job,
            trigger="cron",
            hour=al_h,
            minute=al_m,
            kwargs={"watcher_restart_fn": watcher_restart_fn},
            id="auto_kite_login",
            misfire_grace_time=300,  # run even if up to 5 min late (scheduler thread busy)
        )
        log.info(
            "[scheduler] auto-login scheduled at %s IST",
            settings.KITE_AUTO_LOGIN_TIME,
        )

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
            misfire_grace_time=120,
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
            misfire_grace_time=120,
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
            misfire_grace_time=120,
        )

    if session_factory is not None:
        from app.window_straddle import (
            get_all_entry_jobs, get_all_exit_jobs,
            run_window_straddle_entry, squareoff_window_straddle,
        )
        for _ws_spec in get_all_entry_jobs():
            scheduler.add_job(
                run_window_straddle_entry,
                trigger="cron",
                day_of_week=_ws_spec["day_of_week"],
                hour=_ws_spec["hour"],
                minute=_ws_spec["minute"],
                kwargs={
                    "underlying": _ws_spec["underlying"],
                    "exchange": _ws_spec["exchange"],
                    "qty": _ws_spec["qty"],
                    "monthly_only": _ws_spec["monthly_only"],
                    "entry_hhmm": _ws_spec["entry_hhmm"],
                    "settings": settings,
                    "session_factory": session_factory,
                },
                id=_ws_spec["job_id"],
                misfire_grace_time=120,
            )
            log.info(
                "[scheduler] window straddle entry: %s at %s IST (%s)",
                _ws_spec["underlying"], _ws_spec["entry_hhmm"], _ws_spec["day_of_week"],
            )
        for _ws_und, _ws_exch, _ws_exit_hhmm, _ws_job_id in get_all_exit_jobs():
            _ws_h, _ws_m = _parse_hhmm(_ws_exit_hhmm)
            scheduler.add_job(
                squareoff_window_straddle,
                trigger="cron",
                hour=_ws_h,
                minute=_ws_m,
                kwargs={
                    "underlying": _ws_und,
                    "exchange": _ws_exch,
                    "settings": settings,
                    "session_factory": session_factory,
                },
                id=_ws_job_id,
                misfire_grace_time=120,
            )
            log.info(
                "[scheduler] window straddle exit: %s at %s IST",
                _ws_und, _ws_exit_hhmm,
            )

    # Expiry snapshot jobs — run regardless of DRY_RUN; data collection only, never trades
    from app.expiry_snapshot import run_nse_expiry_snapshot_job, run_mcx_expiry_snapshot_job
    scheduler.add_job(
        run_nse_expiry_snapshot_job,
        trigger="cron",
        hour=15,
        minute=31,
        id="nse_expiry_snapshot",
        misfire_grace_time=300,
    )
    log.info("[scheduler] NSE expiry snapshot scheduled at 15:31 IST (NIFTY/BANKNIFTY/MIDCPNIFTY)")
    scheduler.add_job(
        run_mcx_expiry_snapshot_job,
        trigger="cron",
        hour=22,
        minute=25,
        id="mcx_expiry_snapshot",
        misfire_grace_time=300,
    )
    log.info("[scheduler] MCX expiry snapshot scheduled at 22:25 IST (NATURALGAS/CRUDEOILM)")

    if settings.SCHEDULED_STRADDLE_ENABLED and session_factory is not None:
        from app.scheduled_straddle import run_scheduled_straddle, squareoff_scheduled_straddles

        ng_h, ng_m = _parse_hhmm(settings.NG_STRADDLE_TIME)
        ng_timer = _PreciseTimer(
            "NATURALGAS_straddle", ng_h, ng_m,
            run_scheduled_straddle,
            underlying="NATURALGAS",
            exchange="MCX",
            qty=settings.NG_STRADDLE_QTY,
            settings=settings,
            session_factory=session_factory,
            adx_threshold=settings.NG_STRADDLE_ADX_THRESHOLD,
        )
        ng_timer.start()
        _precise_timers.append(ng_timer)
        log.info(
            "[scheduler] NATURALGAS straddle (PreciseTimer) at %s IST (%d lots, ADX<%s gate)",
            settings.NG_STRADDLE_TIME,
            settings.NG_STRADDLE_QTY,
            settings.NG_STRADDLE_ADX_THRESHOLD,
        )

        sq_h, sq_m = _parse_hhmm(settings.STRADDLE_SQUAREOFF_TIME)
        scheduler.add_job(
            squareoff_scheduled_straddles,
            trigger="cron",
            hour=sq_h,
            minute=sq_m,
            kwargs={
                "settings"       : settings,
                "session_factory": session_factory,
            },
            id="sched_straddle_squareoff",
            misfire_grace_time=120,
        )
        log.info(
            "[scheduler] scheduled straddle squareoff at %s IST",
            settings.STRADDLE_SQUAREOFF_TIME,
        )

    return scheduler
