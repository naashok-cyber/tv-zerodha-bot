"""Morning briefing — the sheet a pro reads before the open, on Telegram.

Per enabled underlying: overnight gap (last price vs previous close), today's
scheduled events with blackout windows, and the latest run's vol read (VRP,
IV trend, regime). Plus the open-position book. Sent once daily at 08:45 IST
when COMMODITY_AGENTS_ENABLED is on.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.commodity_agents import events as events_mod
from app.commodity_agents.models import INDEX_UNDERLYINGS
from app.commodity_agents.notify import send_telegram
from app.commodity_agents.storage import AgentRun
from app.config import IST

log = logging.getLogger(__name__)

_INDEX_SPOT_KEYS = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}


def _quote_key(session: Any, underlying: str, now: datetime) -> str | None:
    if underlying in INDEX_UNDERLYINGS:
        return _INDEX_SPOT_KEYS.get(underlying)
    from app.commodity_agents.strikes import front_month_future
    fut = front_month_future(session, underlying, now.date())
    return f"MCX:{fut.tradingsymbol}" if fut else None


def build_briefing(session: Any, kite: Any, settings: Any,
                   now: datetime | None = None) -> str:
    now = now or datetime.now(IST)
    lines: list[str] = [f"☀️ <b>Morning briefing</b> — {now.strftime('%a %d %b, %H:%M IST')}"]

    keys: dict[str, str] = {}
    for u in settings.COMMODITY_AGENTS_COMMODITIES:
        k = _quote_key(session, u, now)
        if k:
            keys[u] = k
    quotes: dict = {}
    if keys:
        try:
            quotes = kite.quote(list(keys.values()))
        except Exception as exc:
            log.warning("[briefing] quote fetch failed: %s", exc)

    for u in settings.COMMODITY_AGENTS_COMMODITIES:
        parts: list[str] = []
        q = quotes.get(keys.get(u, "")) or {}
        lp = q.get("last_price")
        prev = (q.get("ohlc") or {}).get("close")
        if lp and prev:
            gap = 100.0 * (lp - prev) / prev
            flag = " ⚠️" if abs(gap) >= 1.0 else ""
            parts.append(f"{lp:,.1f} ({gap:+.2f}% vs prev close){flag}")

        run = (
            session.query(AgentRun)
            .filter(AgentRun.commodity == u, AgentRun.analytics_json != None)  # noqa: E711
            .order_by(AgentRun.started_at.desc())
            .first()
        )
        if run:
            quant = json.loads(run.analytics_json or "{}")
            regime = json.loads(run.regime_json or "{}")
            vrp = (quant.get("vrp") or {}).get("vrp_pts")
            trend = (quant.get("iv_trend") or {}).get("direction")
            bits = []
            if regime.get("label"):
                bits.append(regime["label"])
            if vrp is not None:
                bits.append(f"VRP {vrp:+.1f}")
            if trend and trend != "insufficient-history":
                bits.append(f"IV {trend}")
            if bits:
                parts.append(" · ".join(bits))

        evs = events_mod.upcoming_events(
            u, now, horizon_hours=24,
            pre_hours=settings.COMMODITY_BLACKOUT_PRE_HOURS,
            post_hours=settings.COMMODITY_BLACKOUT_POST_HOURS)
        for ev in evs:
            marker = "🔴" if ev.impact == "high" else "🟡"
            parts.append(f"{marker} {ev.name} {ev.event_time.strftime('%H:%M')}")

        if parts:
            lines.append(f"\n<b>{u}</b>: " + " | ".join(parts))

    # open book
    from app.storage import Position
    open_count = (
        session.query(Position)
        .filter(Position.exchange.in_(["MCX", "MCX-OPT", "NFO"]))
        .count()
    )
    lines.append(f"\nOpen positions: {open_count}")
    return "\n".join(lines)


def briefing_job(session_factory: Any, settings: Any) -> None:
    """Scheduler entry point — never raises."""
    from app.kite_session import get_session_manager
    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.warning("[briefing] no Kite session, skipping: %s", exc)
        return
    try:
        with session_factory() as session:
            text = build_briefing(session, kite, settings)
        send_telegram(settings, text)
    except Exception as exc:
        log.error("[briefing] failed: %s", exc)
