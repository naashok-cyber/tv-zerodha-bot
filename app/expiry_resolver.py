from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.config import IST, Settings, get_settings
from app.symbol_mapper import list_expiries


class NoEligibleExpiryError(Exception):
    """Raised when no expiry meets the selection criteria."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolvedExpiry:
    expiry_date: date
    days_to_expiry: int
    rule_used: str      # "NEAREST_WEEKLY" or "NEAREST_MONTHLY"


def _parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def resolve_expiry(
    underlying: str,
    session: Session,
    instrument_type: str = "CE",
    segment: str = "NFO",
    now: datetime | None = None,
    settings: Settings | None = None,
) -> ResolvedExpiry:
    """Select the nearest eligible expiry for an option (or future) trade.

    underlying:      name like "NIFTY", "RELIANCE", "CRUDEOIL"
    session:         SQLAlchemy session for instrument queries
    instrument_type: "CE"/"PE" for options, "FUT" for Natural Gas
    segment:         "NFO", "BFO", "MCX", or "MCX-OPT" — drives cutoff times
    now:             injected for tests; defaults to datetime.now(IST)

    Raises NoEligibleExpiryError when no expiry survives all filters.
    """
    s = settings or get_settings()
    now_ist = now or datetime.now(IST)
    today = now_ist.date()

    weekly_set = {u.upper() for u in s.WEEKLY_INDICES}
    rule = "NEAREST_WEEKLY" if underlying.upper() in weekly_set else "NEAREST_MONTHLY"

    all_expiries = list_expiries(underlying, instrument_type, session)
    future = sorted(e for e in all_expiries if e >= today)

    if not future:
        raise NoEligibleExpiryError(
            f"No future expiries found for {underlying!r} (instrument_type={instrument_type!r})"
        )

    selected = future[0]

    # Skip-expiry-day: if the nearest expiry is today and we've passed the cutoff, advance.
    if selected == today:
        is_mcx = "MCX" in segment.upper()
        cutoff = _parse_hhmm(s.SKIP_EXPIRY_CUTOFF_MCX if is_mcx else s.SKIP_EXPIRY_CUTOFF_NSE)
        if now_ist.time() > cutoff:
            if len(future) < 2:
                raise NoEligibleExpiryError(
                    f"Expiry {selected} is past {cutoff} cutoff and no subsequent expiry exists"
                )
            selected = future[1]

    days_to_expiry = (selected - today).days

    # Minimum days guard.
    upper = underlying.upper()
    min_days = s.MIN_DAYS_TO_EXPIRY_INDEX if upper in weekly_set else s.MIN_DAYS_TO_EXPIRY_STOCK
    if days_to_expiry < min_days:
        raise NoEligibleExpiryError(
            f"Nearest eligible expiry {selected} is only {days_to_expiry} day(s) away; "
            f"minimum is {min_days} for {underlying!r}"
        )

    return ResolvedExpiry(
        expiry_date=selected,
        days_to_expiry=days_to_expiry,
        rule_used=rule,
    )
