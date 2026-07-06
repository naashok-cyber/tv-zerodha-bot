"""LLM calibration scoring — are the debate agents earning their API bill?

About one session after each recommendation, compare the underlying's realized
move against the move the options were pricing at recommendation time:

  * short-premium rec → WIN if the (daily-normalized) realized move stayed
    inside the implied move, LOSS otherwise;
  * NO_TRADE → AVOIDED if the danger materialized (realized > implied),
    MISSED if the market stayed calm and premium was left on the table.

Daily normalization (divide both by sqrt of their day counts) makes the
implied-to-expiry move comparable to a 1-3 day realized window. It is an
approximation — good enough to rank agents, not to settle bets.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from app.commodity_agents.storage import AgentRun, CommodityRecommendation
from app.config import IST

log = logging.getLogger(__name__)

EVAL_AFTER_HOURS = 20          # roughly "next session"
_MAX_EVAL_AGE_DAYS = 7         # stale recs beyond this are skipped (no price ref)

_SHORT_PREMIUM = {"short_straddle", "short_strangle", "short_call", "short_put", "iron_fly"}


def evaluate_pending(session_factory: Any, kite: Any, now: datetime | None = None) -> int:
    """Score recommendations older than EVAL_AFTER_HOURS without an outcome.
    Returns rows scored. Never raises."""
    from app.commodity_agents.orchestrator import _underlying_price

    now = now or datetime.now(IST)
    scored = 0
    with session_factory() as session:
        recs = (
            session.query(CommodityRecommendation)
            .filter(CommodityRecommendation.outcome == None,  # noqa: E711
                    CommodityRecommendation.created_at <= now - timedelta(hours=EVAL_AFTER_HOURS),
                    CommodityRecommendation.created_at >= now - timedelta(days=_MAX_EVAL_AGE_DAYS))
            .all()
        )
        for rec in recs:
            run = session.get(AgentRun, rec.run_pk)
            quant = json.loads((run.analytics_json if run else None) or "{}")
            em = quant.get("expected_move") or {}
            ref_price = em.get("underlying_price")
            implied_pct = em.get("implied_move_pct")
            dte = em.get("days_to_expiry")
            if not ref_price or not implied_pct or not dte:
                rec.outcome = "UNSCORED"     # no quant reference — skip forever
                continue
            try:
                cur = _underlying_price(kite, session, rec.commodity, now)
            except Exception:
                cur = None
            if not cur:
                continue                     # retry next cycle
            realized_pct = abs(cur - ref_price) / ref_price * 100.0
            created = (rec.created_at if rec.created_at.tzinfo
                       else rec.created_at.replace(tzinfo=IST))   # SQLite drops tz
            days = max((now - created).total_seconds() / 86400.0, 0.5)
            realized_daily = realized_pct / math.sqrt(days)
            implied_daily = implied_pct / math.sqrt(max(dte, 0.5))
            danger = realized_daily > implied_daily
            if rec.strategy_type in _SHORT_PREMIUM:
                rec.outcome = "LOSS" if danger else "WIN"
            else:                            # NO_TRADE / directional
                rec.outcome = "AVOIDED" if danger else "MISSED"
            rec.realized_move_pct = round(realized_pct, 3)
            scored += 1
        session.commit()
    if scored:
        log.info("[calibration] scored %d recommendation(s)", scored)
    return scored


def calibration_stats(session: Any) -> dict:
    """Hit rates for the judge and risk-flag reliability per debate agent."""
    recs = (
        session.query(CommodityRecommendation)
        .filter(CommodityRecommendation.outcome.in_(["WIN", "LOSS", "AVOIDED", "MISSED"]))
        .all()
    )
    judge = {"n": 0, "wins": 0, "conf_sum_win": 0.0, "conf_sum_loss": 0.0, "losses": 0}
    agent_flags: dict[str, dict[str, dict[str, int]]] = {
        a: {} for a in ("trend", "event", "vol")
    }
    no_trade = {"avoided": 0, "missed": 0}

    for rec in recs:
        if rec.outcome in ("AVOIDED", "MISSED"):
            no_trade["avoided" if rec.outcome == "AVOIDED" else "missed"] += 1
        else:
            judge["n"] += 1
            if rec.outcome == "WIN":
                judge["wins"] += 1
                judge["conf_sum_win"] += rec.confidence or 0.0
            else:
                judge["losses"] += 1
                judge["conf_sum_loss"] += rec.confidence or 0.0
        run = rec.run
        if run is None:
            continue
        # danger materialized ⇔ LOSS or AVOIDED
        danger = rec.outcome in ("LOSS", "AVOIDED")
        for agent, field in (("trend", run.trend_json), ("event", run.event_debate_json),
                             ("vol", run.vol_json)):
            try:
                flag = (json.loads(field or "{}") or {}).get("risk_flag")
            except ValueError:
                flag = None
            if flag not in ("low", "medium", "high"):
                continue
            bucket = agent_flags[agent].setdefault(flag, {"n": 0, "danger": 0})
            bucket["n"] += 1
            bucket["danger"] += 1 if danger else 0

    out: dict = {
        "judge": {
            "actionable_recs": judge["n"],
            "win_rate_pct": round(100.0 * judge["wins"] / judge["n"], 1) if judge["n"] else None,
            "avg_confidence_on_wins": round(judge["conf_sum_win"] / judge["wins"], 2) if judge["wins"] else None,
            "avg_confidence_on_losses": round(judge["conf_sum_loss"] / judge["losses"], 2) if judge["losses"] else None,
        },
        "no_trade": no_trade,
        # For a well-calibrated agent, danger rate should RISE with the flag:
        # high-flag runs should see danger far more often than low-flag runs.
        "risk_flag_reliability": {
            agent: {
                flag: {"n": b["n"],
                       "danger_rate_pct": round(100.0 * b["danger"] / b["n"], 1)}
                for flag, b in flags.items()
            }
            for agent, flags in agent_flags.items()
        },
    }
    return out


def calibration_job(session_factory: Any) -> None:
    """Scheduler entry point — never raises."""
    from app.kite_session import get_session_manager
    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.warning("[calibration] no Kite session, skipping: %s", exc)
        return
    try:
        evaluate_pending(session_factory, kite)
    except Exception as exc:
        log.error("[calibration] evaluation failed: %s", exc)
