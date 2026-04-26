"""Tests for app/expiry_resolver.py — all offline, no network."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import IST, Settings
from app.expiry_resolver import NoEligibleExpiryError, ResolvedExpiry, resolve_expiry


def _s(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _now(d: date, h: int, m: int) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def _mock_list_expiries(expiries: list[date]):
    """Return a patcher that replaces list_expiries with a fixed list."""
    return expiries


# ── NEAREST_WEEKLY (NIFTY) ────────────────────────────────────────────────────

def test_weekly_picks_nearest_nifty(monkeypatch):
    # Expiries: last week (expired), this Thursday, next Thursday.
    today = date(2025, 1, 9)           # Thursday
    available = [date(2025, 1, 2), date(2025, 1, 16), date(2025, 1, 23)]
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: available)
    now = _now(today, 10, 0)

    result = resolve_expiry("NIFTY", session=None, now=now, settings=_s())  # type: ignore[arg-type]

    assert result.expiry_date == date(2025, 1, 16)
    assert result.rule_used == "NEAREST_WEEKLY"
    assert result.days_to_expiry == 7


# ── NEAREST_MONTHLY (RELIANCE) ────────────────────────────────────────────────

def test_monthly_picks_nearest_reliance(monkeypatch):
    today = date(2025, 1, 9)
    available = [date(2025, 1, 30), date(2025, 2, 27)]
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: available)
    now = _now(today, 10, 0)

    result = resolve_expiry("RELIANCE", session=None, now=now, settings=_s())  # type: ignore[arg-type]

    assert result.expiry_date == date(2025, 1, 30)
    assert result.rule_used == "NEAREST_MONTHLY"


# ── Skip-expiry-day NSE ───────────────────────────────────────────────────────

def test_skip_expiry_nse_after_cutoff(monkeypatch):
    # Expiry day = today; current time 14:35 IST > 14:30 cutoff → advance to next week.
    today = date(2025, 1, 16)
    available = [today, date(2025, 1, 23)]
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: available)
    now = _now(today, 14, 35)   # 5 min past NSE cutoff

    result = resolve_expiry("NIFTY", session=None, segment="NFO", now=now, settings=_s())  # type: ignore[arg-type]

    assert result.expiry_date == date(2025, 1, 23)
    assert result.days_to_expiry == 7


def test_no_skip_expiry_nse_before_cutoff(monkeypatch):
    # Same day, before cutoff → keep today's expiry (no roll).
    today = date(2025, 1, 16)
    available = [today, date(2025, 1, 23)]
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: available)
    now = _now(today, 14, 0)   # 30 min before NSE cutoff

    # Allow 0 days so the min-days guard doesn't interfere with this specific test.
    result = resolve_expiry(
        "NIFTY", session=None, segment="NFO", now=now,  # type: ignore[arg-type]
        settings=_s(MIN_DAYS_TO_EXPIRY_INDEX=0),
    )

    assert result.expiry_date == today
    assert result.days_to_expiry == 0


# ── Skip-expiry-day MCX ───────────────────────────────────────────────────────

def test_skip_expiry_mcx_after_cutoff(monkeypatch):
    today = date(2025, 1, 17)
    available = [today, date(2025, 2, 19)]
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: available)
    now = _now(today, 22, 30)   # past 22:00 MCX cutoff

    result = resolve_expiry(
        "CRUDEOIL", session=None, segment="MCX", now=now, settings=_s()  # type: ignore[arg-type]
    )

    assert result.expiry_date == date(2025, 2, 19)


# ── MIN_DAYS_TO_EXPIRY ────────────────────────────────────────────────────────

def test_min_days_index_raises(monkeypatch):
    # MIN_DAYS_TO_EXPIRY_INDEX=1 → expiry today with days=0 → raise (NIFTY index).
    # But we're before the cutoff, so today IS selected, and 0 < 1 → raise.
    today = date(2025, 1, 16)
    available = [today]
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: available)
    now = _now(today, 10, 0)

    with pytest.raises(NoEligibleExpiryError, match="0 day"):
        resolve_expiry(
            "NIFTY", session=None, segment="NFO", now=now,
            settings=_s(MIN_DAYS_TO_EXPIRY_INDEX=1),  # type: ignore[arg-type]
        )


def test_min_days_stock_raises(monkeypatch):
    # MIN_DAYS_TO_EXPIRY_STOCK=3; nearest expiry is 2 days away → raise.
    today = date(2025, 1, 9)
    available = [date(2025, 1, 11)]  # 2 days away
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: available)
    now = _now(today, 10, 0)

    with pytest.raises(NoEligibleExpiryError, match="minimum is 3"):
        resolve_expiry(
            "RELIANCE", session=None, segment="NFO", now=now,
            settings=_s(MIN_DAYS_TO_EXPIRY_STOCK=3),  # type: ignore[arg-type]
        )


# ── Empty expiry list ─────────────────────────────────────────────────────────

def test_empty_expiry_list_raises(monkeypatch):
    monkeypatch.setattr("app.expiry_resolver.list_expiries", lambda *_: [])
    now = _now(date(2025, 1, 9), 10, 0)

    with pytest.raises(NoEligibleExpiryError, match="No future expiries"):
        resolve_expiry("NIFTY", session=None, now=now, settings=_s())  # type: ignore[arg-type]
