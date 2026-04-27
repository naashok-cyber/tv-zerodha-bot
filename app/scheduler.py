from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

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


def make_scheduler(settings: Any = None) -> BackgroundScheduler:
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
    return scheduler
