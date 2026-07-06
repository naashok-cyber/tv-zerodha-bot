"""Weekly event-calendar verification via LLM web search.

The static calendar in events.py is the biggest known hole: the backtest's
worst days were event gaps the 2026-only hardcoded lists could not see, and
the RBI MPC dates are marked APPROXIMATE. This job asks the (web-search
enabled) event model to verify the next 45 days of scheduled macro events and
writes the result to data/commodity_events.json under "auto_events".

Safety posture: the LLM can only ADD blackout-worthy events, never remove the
hardcoded ones — events.py unions all sources, and every entry is validated
strictly before it is written. Manual "extra_events" entries are preserved.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from app.commodity_agents.events import EVENTS_OVERRIDE_PATH
from app.commodity_agents.llm_client import ROLE_EVENT, LlmClient, LlmError
from app.commodity_agents.models import COMMODITIES
from app.config import IST

log = logging.getLogger(__name__)

_MAX_AUTO_EVENTS = 20
_HORIZON_DAYS = 45

_PROMPT = """Verify the scheduled high-impact macro events for Indian commodity and
index options over the next {days} days (today is {today} IST). Use web search to
confirm EXACT dates for:
- RBI Monetary Policy Committee decision days (affects NIFTY, BANKNIFTY)
- US FOMC rate decision days (affects GOLD, SILVER, NIFTY, BANKNIFTY)
- US CPI release dates (affects GOLD, SILVER)
- OPEC+ meeting dates (affects CRUDEOIL)
- Any EIA report date shifted by a US holiday (affects CRUDEOIL, NATURALGAS)

Convert all times to IST. Return ONLY a JSON object, no prose:
{{"events": [{{"name": "<event name>", "commodities": [<subset of {commodities}>],
"datetime_ist": "YYYY-MM-DDTHH:MM", "impact": "high" | "medium"}}]}}
Only include events you confirmed with a source. If unsure of the time, use the
usual release time for that series."""


def _validate(entry: Any, now: datetime) -> dict | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    commodities = entry.get("commodities")
    impact = entry.get("impact")
    dt_raw = entry.get("datetime_ist")
    if not (isinstance(name, str) and name.strip()):
        return None
    if not (isinstance(commodities, list)
            and commodities
            and all(c in COMMODITIES for c in commodities)):
        return None
    if impact not in ("high", "medium"):
        return None
    try:
        dt = datetime.fromisoformat(dt_raw).replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None
    if not (now - timedelta(days=1) <= dt <= now + timedelta(days=_HORIZON_DAYS + 5)):
        return None
    return {"name": name.strip()[:80], "commodities": commodities,
            "datetime_ist": dt.strftime("%Y-%m-%dT%H:%M"), "impact": impact}


def refresh_event_calendar(settings: Any, now: datetime | None = None) -> int:
    """Returns the number of auto events written, -1 on skip/failure."""
    if not settings.ANTHROPIC_API_KEY or not settings.COMMODITY_AGENTS_WEB_SEARCH:
        log.info("[calendar] refresh skipped (no API key or web search disabled)")
        return -1
    now = now or datetime.now(IST)
    llm = LlmClient(
        api_key=settings.ANTHROPIC_API_KEY,
        role_models={ROLE_EVENT: settings.COMMODITY_AGENT_MODEL_EVENT},
        enable_web_search=True,
    )
    prompt = _PROMPT.format(days=_HORIZON_DAYS, today=now.strftime("%Y-%m-%d"),
                            commodities=list(COMMODITIES))
    try:
        out = llm.run(ROLE_EVENT, "You are a precise financial-calendar verifier.", prompt)
    except LlmError as exc:
        log.error("[calendar] LLM refresh failed: %s", exc)
        return -1

    validated = []
    for entry in (out.get("events") or [])[:_MAX_AUTO_EVENTS]:
        v = _validate(entry, now)
        if v:
            validated.append(v)

    existing: dict = {}
    if os.path.exists(EVENTS_OVERRIDE_PATH):
        try:
            with open(EVENTS_OVERRIDE_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    existing["auto_events"] = validated
    existing["auto_refreshed_at"] = now.isoformat()
    os.makedirs(os.path.dirname(EVENTS_OVERRIDE_PATH) or ".", exist_ok=True)
    with open(EVENTS_OVERRIDE_PATH, "w") as f:
        json.dump(existing, f, indent=1)
    log.info("[calendar] wrote %d auto event(s)", len(validated))
    return len(validated)


def calendar_refresh_job(settings: Any) -> None:
    """Scheduler entry point — never raises."""
    try:
        refresh_event_calendar(settings)
    except Exception as exc:
        log.error("[calendar] refresh job failed: %s", exc)
