"""Per-commodity event calendar with blackout-window logic.

The mapping of events to commodities is HARDCODED deliberately (Section 2 of
the design brief): an EIA gas storage report is irrelevant to gold, a FOMC
meeting is irrelevant to natural gas — never let an LLM guess this mapping.

Recurring rules (EIA weekly reports, NFP first-Friday) are generated
programmatically. Date-specific events (FOMC, CPI) live in static lists below
and can be overridden/extended via data/commodity_events.json:

    {"extra_events": [{"name": "US CPI", "commodities": ["GOLD", "SILVER"],
                       "datetime_ist": "2026-08-12T18:00"}]}
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from app.config import IST
from app.commodity_agents.models import EventWindow

log = logging.getLogger(__name__)

EVENTS_OVERRIDE_PATH = "data/commodity_events.json"

# ── Recurring weekly events (IST times; US EIA releases 10:30/11:00 ET) ──────
# weekday: Monday=0 … Sunday=6
_WEEKLY_EVENTS = [
    # EIA Natural Gas Storage Report — Thursdays 10:30 ET ≈ 20:00 IST (winter 21:00)
    {"name": "EIA Natural Gas Storage Report", "commodity": "NATURALGAS",
     "weekday": 3, "hhmm": (20, 0), "impact": "high"},
    # EIA Weekly Petroleum Status — Wednesdays 10:30 ET ≈ 20:00 IST
    {"name": "EIA Crude Oil Inventory", "commodity": "CRUDEOIL",
     "weekday": 2, "hhmm": (20, 0), "impact": "high"},
    # Baker Hughes rig count — Fridays 13:00 ET ≈ 22:30 IST (medium impact)
    {"name": "Baker Hughes Rig Count", "commodity": "CRUDEOIL",
     "weekday": 4, "hhmm": (22, 30), "impact": "medium"},
]

# US Non-Farm Payrolls: first Friday of the month, 08:30 ET ≈ 18:00 IST.
_NFP_COMMODITIES = ("GOLD", "SILVER")
_NFP_HHMM = (18, 0)

# ── Date-specific events ─────────────────────────────────────────────────────
# FOMC 2026 decision days (second day of each meeting), statement ~14:00 ET
# ≈ 23:30 IST. VERIFY against federalreserve.gov when the year rolls over.
# Indices carry FOMC too: the decision lands after NSE close, so the risk is
# an overnight gap into the next session — still blackout-worthy for short
# premium held overnight.
_FOMC_DATES_2026 = [
    (2026, 1, 28), (2026, 3, 18), (2026, 4, 29), (2026, 6, 17),
    (2026, 7, 29), (2026, 9, 16), (2026, 10, 28), (2026, 12, 9),
]
_FOMC_COMMODITIES = ("GOLD", "SILVER", "NIFTY", "BANKNIFTY")
_FOMC_HHMM = (23, 30)

# RBI MPC 2026 policy announcement days, ~10:00 IST. APPROXIMATE — the RBI
# publishes the bi-monthly calendar; VERIFY at rbi.org.in and correct via the
# data/commodity_events.json override if these drift.
_RBI_DATES_2026 = [
    (2026, 2, 6), (2026, 4, 9), (2026, 6, 5),
    (2026, 8, 6), (2026, 10, 1), (2026, 12, 4),
]
_RBI_COMMODITIES = ("NIFTY", "BANKNIFTY")
_RBI_HHMM = (10, 0)

# US CPI release dates vary month to month — maintain via the JSON override
# file rather than hardcoding guesses here.


def _load_override_events() -> list[dict]:
    if not os.path.exists(EVENTS_OVERRIDE_PATH):
        return []
    try:
        with open(EVENTS_OVERRIDE_PATH) as f:
            data = json.load(f)
        # extra_events = manual entries; auto_events = weekly LLM-verified
        # calendar refresh. Both only ADD events — the hardcoded calendar
        # above is never removed by either source.
        return data.get("extra_events", []) + data.get("auto_events", [])
    except Exception as exc:
        log.warning("[events] could not parse %s: %s", EVENTS_OVERRIDE_PATH, exc)
        return []


def _first_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=IST)
    offset = (4 - d.weekday()) % 7
    return d + timedelta(days=offset)


def upcoming_events(
    commodity: str,
    now: datetime,
    horizon_hours: float = 96.0,
    pre_hours: float = 3.0,
    post_hours: float = 1.0,
) -> list[EventWindow]:
    """All events for `commodity` whose event_time falls within the horizon."""
    horizon_end = now + timedelta(hours=horizon_hours)
    out: list[EventWindow] = []

    def _add(name: str, event_time: datetime, impact: str) -> None:
        # include events slightly in the past so a just-fired event's
        # post-blackout still registers
        if now - timedelta(hours=post_hours) <= event_time <= horizon_end:
            out.append(EventWindow(
                name=name,
                commodity=commodity,
                event_time=event_time,
                blackout_start=event_time - timedelta(hours=pre_hours),
                blackout_end=event_time + timedelta(hours=post_hours),
                impact=impact,
            ))

    # weekly recurring
    for spec in _WEEKLY_EVENTS:
        if spec["commodity"] != commodity:
            continue
        for day_offset in range(0, int(horizon_hours // 24) + 2):
            d = (now + timedelta(days=day_offset)).astimezone(IST)
            if d.weekday() == spec["weekday"]:
                h, m = spec["hhmm"]
                _add(spec["name"], d.replace(hour=h, minute=m, second=0, microsecond=0), spec["impact"])

    # NFP first Friday
    if commodity in _NFP_COMMODITIES:
        for month_probe in (now, now + timedelta(days=32)):
            ff = _first_friday(month_probe.year, month_probe.month)
            h, m = _NFP_HHMM
            _add("US Non-Farm Payrolls", ff.replace(hour=h, minute=m), "high")

    # FOMC static dates
    if commodity in _FOMC_COMMODITIES:
        h, m = _FOMC_HHMM
        for (y, mo, dd) in _FOMC_DATES_2026:
            _add("FOMC Rate Decision", datetime(y, mo, dd, h, m, tzinfo=IST), "high")

    # RBI MPC static dates (indices)
    if commodity in _RBI_COMMODITIES:
        h, m = _RBI_HHMM
        for (y, mo, dd) in _RBI_DATES_2026:
            _add("RBI MPC Policy Decision", datetime(y, mo, dd, h, m, tzinfo=IST), "high")

    # JSON overrides
    for extra in _load_override_events():
        if commodity not in extra.get("commodities", []):
            continue
        try:
            et = datetime.fromisoformat(extra["datetime_ist"]).replace(tzinfo=IST)
        except Exception:
            log.warning("[events] bad datetime_ist in override entry: %r", extra)
            continue
        _add(extra.get("name", "custom event"), et, extra.get("impact", "high"))

    # de-dupe (same name + time can appear twice from weekday scan overlap)
    seen: set[tuple] = set()
    unique = []
    for ev in sorted(out, key=lambda e: e.event_time):
        key = (ev.name, ev.event_time)
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique


def active_blackout(
    commodity: str,
    now: datetime,
    pre_hours: float = 3.0,
    post_hours: float = 1.0,
) -> EventWindow | None:
    """Return the high-impact event whose blackout window contains `now`, if any."""
    for ev in upcoming_events(commodity, now, horizon_hours=pre_hours + 1,
                              pre_hours=pre_hours, post_hours=post_hours):
        if ev.impact == "high" and ev.blackout_start <= now <= ev.blackout_end:
            return ev
    return None
