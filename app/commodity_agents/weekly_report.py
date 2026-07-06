"""Sunday-evening desk review on Telegram.

What a pro reads before the new week: expectancy by regime, judge calibration,
execution quality (slippage), and where each market's IV sits versus its own
history. All composed from data the system already records — no API calls.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from app.commodity_agents.calibration import calibration_stats
from app.commodity_agents.journal import expectancy_stats
from app.commodity_agents.notify import send_telegram
from app.commodity_agents.storage import AgentRun, CommodityTradeJournal
from app.config import IST

log = logging.getLogger(__name__)


def build_weekly_report(session: Any, settings: Any,
                        now: datetime | None = None) -> str:
    now = now or datetime.now(IST)
    week_ago = now - timedelta(days=7)
    lines = [f"📒 <b>Weekly desk review</b> — week ending {now:%d %b}"]

    # trades this week
    week_rows = (
        session.query(CommodityTradeJournal)
        .filter(CommodityTradeJournal.entered_at >= week_ago)
        .all()
    )
    closed = [r for r in week_rows if r.realized_pnl is not None]
    if week_rows:
        total = sum(r.realized_pnl for r in closed)
        wins = sum(1 for r in closed if r.realized_pnl > 0)
        lines.append(f"\nApprovals: {len(week_rows)} "
                     f"({sum(1 for r in week_rows if r.mode == 'live')} live) · "
                     f"closed {len(closed)}"
                     + (f" · {wins}/{len(closed)} wins · net ₹{total:+,.0f}"
                        if closed else ""))
        slips = [r.slippage_pct for r in closed if r.slippage_pct is not None]
        if slips:
            lines.append(f"Avg entry slippage: {sum(slips) / len(slips):+.2f}% "
                         f"(negative = fills worse than the analysis quote)")
        maes = [r.mae_pct for r in closed if r.mae_pct is not None]
        if maes:
            lines.append(f"Worst heat taken (MAE): {max(maes):.0f}% of entry premium")
    else:
        lines.append("\nNo approvals this week.")

    # all-time expectancy by regime (the number that decides the gate)
    exp = expectancy_stats(session)
    if exp:
        lines.append("\n<b>Expectancy by regime (all time)</b>")
        for regime, s in sorted(exp.items()):
            lines.append(f"{regime}: {s['trades']} trades · {s['win_rate_pct']:.0f}% win "
                         f"· mean ₹{s['mean_pnl']:+,} · worst ₹{s['worst']:,}")

    # LLM calibration
    cal = calibration_stats(session)
    judge = cal.get("judge") or {}
    if judge.get("actionable_recs"):
        lines.append(f"\n<b>Judge</b>: {judge['win_rate_pct']:.0f}% win over "
                     f"{judge['actionable_recs']} scored calls")
    nt = cal.get("no_trade") or {}
    if nt.get("avoided") or nt.get("missed"):
        lines.append(f"NO-TRADE calls: {nt['avoided']} avoided danger, "
                     f"{nt['missed']} missed calm markets")

    # IV standing per underlying (latest run vs its own stored history)
    iv_lines = []
    for commodity in settings.COMMODITY_AGENTS_COMMODITIES:
        run = (
            session.query(AgentRun)
            .filter(AgentRun.commodity == commodity,
                    AgentRun.regime_json != None)  # noqa: E711
            .order_by(AgentRun.started_at.desc())
            .first()
        )
        if run is None:
            continue
        regime = json.loads(run.regime_json or "{}")
        ivp = regime.get("iv_percentile")
        if ivp is not None:
            iv_lines.append(f"{commodity}: IV {regime.get('atm_iv', 0) * 100:.0f}% "
                            f"(pctile {ivp:.0f}) · {regime.get('label')}")
    if iv_lines:
        lines.append("\n<b>Where IV sits</b>")
        lines.extend(iv_lines)

    return "\n".join(lines)


def weekly_report_job(session_factory: Any, settings: Any) -> None:
    """Scheduler entry point — never raises."""
    try:
        with session_factory() as session:
            text = build_weekly_report(session, settings)
        send_telegram(settings, text)
    except Exception as exc:
        log.error("[weekly_report] failed: %s", exc)
