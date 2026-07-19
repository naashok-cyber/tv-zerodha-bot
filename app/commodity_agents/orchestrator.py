"""Pipeline orchestrator: deterministic layers → LLM debate → judge → risk guard.

One run per commodity. Every stage's output is persisted to AgentRun as it
completes, so a crash mid-run still leaves an auditable partial trail.

Cost control: the LLM round is skipped entirely (status SKIPPED_REGIME) when
the deterministic regime classifier says high-vol-expansion — there is nothing
to debate about selling premium into a vol blowout.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.commodity_agents import analytics as analytics_mod
from app.commodity_agents import debate, events, regime as regime_mod, risk_guard, strikes
from app.commodity_agents.llm_client import LlmClient, LlmError
from app.commodity_agents.models import (
    COMMODITIES,
    CORRELATION_GROUPS,
    HIGH_VOL_EXPANSION,
    INDEX_UNDERLYINGS,
    TRENDING,
    exchange_for,
    group_for,
)
from app.commodity_agents.storage import AgentRun, CommodityRecommendation
from app.config import IST
from app.storage import Position

log = logging.getLogger(__name__)

# 30-minute candles: ~14h MCX session ≈ 28/day; ~6.25h NSE session ≈ 13/day
_CANDLE_INTERVAL = "30minute"
_CANDLE_DAYS = 7
_PERIODS_PER_YEAR = 28 * 252            # MCX (kept name — used by backtest too)
_PERIODS_PER_YEAR_NSE = 13 * 252

# Kite quote keys for index spot
_INDEX_SPOT_KEYS = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}

# Entry-relevant market hours (IST) per exchange; runs outside are skipped.
_MARKET_HOURS = {"MCX": ((9, 0), (23, 30)), "NFO": ((9, 15), (15, 30))}


def market_open(underlying: str, now: datetime) -> bool:
    if now.weekday() >= 5:      # Sat/Sun
        return False
    (oh, om), (ch, cm) = _MARKET_HOURS[exchange_for(underlying)]
    minutes = now.hour * 60 + now.minute
    return oh * 60 + om <= minutes <= ch * 60 + cm


def _periods_per_year(underlying: str) -> float:
    return _PERIODS_PER_YEAR_NSE if underlying in INDEX_UNDERLYINGS else _PERIODS_PER_YEAR
# Short-premium worst-case heuristic: assume exit at SL = 100% of collected
# premium (premium doubles). est_max_loss/lot = combined ATM premium × units.
_SL_MULTIPLE = 1.0
_IV_HISTORY_LIMIT = 500


def _fetch_candles(kite: Any, session: Any, commodity: str, now: datetime) -> list[dict] | None:
    fut = strikes.front_month_future(session, commodity, now.date())
    if fut is None:
        log.warning("[commodity_agents] no front-month future for %s", commodity)
        return None
    from_dt = now - timedelta(days=_CANDLE_DAYS)
    raw = kite.historical_data(fut.instrument_token, from_dt, now, _CANDLE_INTERVAL)
    return raw or None


def _underlying_price(kite: Any, session: Any, commodity: str, now: datetime) -> float | None:
    """Greeks reference price: index spot for NFO, front-future LTP for MCX."""
    if commodity in INDEX_UNDERLYINGS:
        key = _INDEX_SPOT_KEYS[commodity]
    else:
        fut = strikes.front_month_future(session, commodity, now.date())
        if fut is None:
            return None
        key = f"MCX:{fut.tradingsymbol}"
    data = kite.ltp([key])
    lp = (data.get(key) or {}).get("last_price")
    return float(lp) if lp else None


def _india_vix(kite: Any) -> float | None:
    try:
        data = kite.ltp(["NSE:INDIA VIX"])
        lp = (data.get("NSE:INDIA VIX") or {}).get("last_price")
        return float(lp) if lp else None
    except Exception:
        return None


def _iv_history(session: Any, commodity: str) -> list[float]:
    rows = (
        session.query(AgentRun.atm_iv)
        .filter(AgentRun.commodity == commodity, AgentRun.atm_iv != None)  # noqa: E711
        .order_by(AgentRun.started_at.desc())
        .limit(_IV_HISTORY_LIMIT)
        .all()
    )
    return [r[0] for r in rows]


def _daily_realized_pnl(session: Any, now: datetime) -> float:
    from sqlalchemy import func
    from app.storage import ClosedTrade
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = (
        session.query(func.coalesce(func.sum(ClosedTrade.pnl), 0.0))
        .filter(ClosedTrade.closed_at >= day_start)
        .scalar()
    )
    return float(total or 0.0)


def _open_commodity_count(session: Any) -> int:
    rows = (
        session.query(Position.underlying)
        .filter(Position.exchange.in_(["MCX", "MCX-OPT", "NFO"]),
                Position.underlying.in_(list(COMMODITIES)))
        .distinct()
        .all()
    )
    return len(rows)


def _open_group_shorts(session: Any, commodity: str) -> int:
    """Distinct OTHER underlyings in the same correlation group with open
    short option positions (Order.transaction_type == SELL)."""
    group = group_for(commodity)
    if group is None:
        return 0
    from app.storage import Order
    members = [m for m in CORRELATION_GROUPS[group] if m != commodity]
    if not members:
        return 0
    rows = (
        session.query(Position.underlying)
        .join(Order, Position.order_id == Order.id)
        .filter(
            Position.underlying.in_(members),
            Position.instrument_type.in_(["CE", "PE"]),
            Order.transaction_type == "SELL",
        )
        .distinct()
        .all()
    )
    return len(rows)


def _margin_available(kite: Any, commodity: str) -> float | None:
    segment = "equity" if commodity in INDEX_UNDERLYINGS else "commodity"
    try:
        margins = kite.margins()
        return float(margins[segment]["net"])
    except Exception:
        return None


def _margin_required_basket(kite: Any, commodity: str, ce: Any, pe: Any,
                            lots: int = 1) -> float | None:
    """Broker-side combined margin for the two short legs (hedge benefit
    included). None on any failure — the risk guard then skips the check."""
    exch = exchange_for(commodity)
    # NFO order quantity is units (lots × lot_size); MCX option quantity is
    # lots (instrument lot_size=1 — MCX_LOT_UNITS is a PnL multiplier only).
    qty_per_lot = ce.lot_units if exch == "NFO" else 1
    legs = [
        {
            "exchange": exch,
            "tradingsymbol": c.tradingsymbol,
            "transaction_type": "SELL",
            "variety": "regular",
            "product": "NRML",
            "order_type": "MARKET",
            "quantity": lots * qty_per_lot,
            "price": 0,
            "trigger_price": 0,
        }
        for c in (ce, pe)
    ]
    try:
        data = kite.basket_order_margins(legs)
        total = (data.get("final") or {}).get("total")
        return float(total) if total is not None else None
    except Exception as exc:
        log.warning("[commodity_agents] basket margin lookup failed for %s: %s",
                    commodity, exc)
        return None


def run_commodity_pipeline(
    commodity: str,
    session_factory: Any,
    settings: Any,
    kite: Any,
    llm: LlmClient | None,
    now: datetime | None = None,
) -> int | None:
    """Run the full pipeline for one commodity. Returns AgentRun.id (or None
    if it could not even start). Never raises — failures land in run.error."""
    now = now or datetime.now(IST)
    run = AgentRun(run_id=str(uuid.uuid4()), commodity=commodity,
                   started_at=now, status="RUNNING")

    with session_factory() as session:
        session.add(run)
        session.commit()
        run_id = run.id
        try:
            _run_stages(run, session, settings, kite, llm, commodity, now)
        except Exception as exc:
            log.error("[commodity_agents] %s pipeline failed: %s", commodity, exc, exc_info=True)
            run.status = "FAILED"
            run.error = str(exc)
        run.finished_at = datetime.now(IST)
        session.commit()
    return run_id


def _run_stages(
    run: AgentRun, session: Any, settings: Any, kite: Any,
    llm: LlmClient | None, commodity: str, now: datetime,
) -> None:
    pre_h = settings.COMMODITY_BLACKOUT_PRE_HOURS
    post_h = settings.COMMODITY_BLACKOUT_POST_HOURS

    # 1. Event calendar + blackout
    evs = events.upcoming_events(commodity, now, pre_hours=pre_h, post_hours=post_h)
    blackout = events.active_blackout(commodity, now, pre_hours=pre_h, post_hours=post_h)
    run.events_json = json.dumps([
        {"name": e.name, "time_ist": e.event_time.isoformat(),
         "impact": e.impact, "blackout_active": e == blackout}
        for e in evs
    ])
    session.commit()

    # 2. Market data
    candles = _fetch_candles(kite, session, commodity, now)
    if not candles:
        raise RuntimeError(f"no candles for {commodity}")
    fut_price = _underlying_price(kite, session, commodity, now)
    if not fut_price:
        raise RuntimeError(f"no underlying price for {commodity}")

    # 3. Strike candidates + ATM IV + chain positioning (OI) in one quote round
    cands, positioning = strikes.build_chain_snapshot(
        session, kite.quote, commodity, fut_price, now)
    run.strikes_json = json.dumps([c.to_dict() for c in cands])
    atm_iv = strikes.atm_iv_from_candidates(cands)
    run.atm_iv = atm_iv
    session.commit()

    # 4. Regime
    snap = regime_mod.classify_regime(
        candles=candles,
        periods_per_year=_periods_per_year(commodity),
        atm_iv=atm_iv,
        iv_history=_iv_history(session, commodity),
        in_event_window=blackout is not None,
        adx_period=settings.ADX_PERIOD,
    )
    run.regime_json = json.dumps(snap.to_dict())
    session.commit()

    # 4a. Regime-flip alert: if the market just changed character AGAINST an
    # open short-premium position, the trader hears about it now — not at the
    # stop. Deterministic, Telegram-only, never blocks the pipeline.
    try:
        _regime_flip_alert(session, settings, run, commodity, snap.label)
    except Exception as exc:
        log.warning("[commodity_agents] regime-flip alert failed: %s", exc)

    # 4b. Vol analytics: VRP, IV trend, expected move vs breakevens, stress.
    # _iv_history already includes this run's atm_iv (committed at stage 3),
    # oldest sample last after reversal — exactly what iv_trend expects.
    next_iv = None
    if cands:
        try:
            near_exp = datetime.strptime(cands[0].expiry, "%Y-%m-%d").date()
            next_iv = strikes.next_expiry_atm_iv(
                session, kite.quote, commodity, fut_price, near_exp, now=now)
        except Exception as exc:
            log.debug("[commodity_agents] next-expiry IV unavailable for %s: %s",
                      commodity, exc)
    quant = analytics_mod.compute_analytics(
        commodity=commodity,
        candidates=cands,
        atm_iv=atm_iv,
        realized_vol=snap.realized_vol,
        future_price=fut_price,
        now=now,
        iv_history_asc=list(reversed(_iv_history(session, commodity))),
        risk_free_rate=settings.RISK_FREE_RATE,
        next_atm_iv=next_iv,
        positioning=positioning or None,
        india_vix=_india_vix(kite) if commodity in INDEX_UNDERLYINGS else None,
    )
    run.analytics_json = json.dumps(quant)
    session.commit()

    # 5. Regime gate — skip the (paid) LLM round when there's nothing to debate
    if snap.label == HIGH_VOL_EXPANSION:
        run.status = "SKIPPED_REGIME"
        _persist_recommendation(
            session, run, commodity, now,
            direction="NO_TRADE", strategy_type="none", strikes_syms=[],
            confidence=1.0,
            reasoning="Regime gate: high-vol-expansion — short-premium debate skipped.",
            dissent=[], risk_vetoed=False, settings=settings,
        )
        return

    if llm is None:
        raise RuntimeError("LLM client unavailable (ANTHROPIC_API_KEY not set?)")

    # 6. Debate round
    ctx = debate._fmt_context(commodity, snap, debate.summarize_candles(candles),
                              cands, evs, analytics=quant)
    trend_out = llm_stage(run, session, "trend_json", lambda: debate.run_trend_agent(llm, ctx))
    event_out = llm_stage(run, session, "event_debate_json", lambda: debate.run_event_agent(llm, ctx))
    vol_out = llm_stage(run, session, "vol_json", lambda: debate.run_vol_agent(llm, ctx))

    # 7. Judge + code-level gap-risk gate
    judge_out = debate.run_judge(llm, ctx, trend_out, event_out, vol_out)
    judge_out = debate.enforce_gap_risk_gate(judge_out, event_out)
    run.judge_json = json.dumps(judge_out)
    session.commit()

    # 8. Risk Guard (deterministic, final say)
    strategy = judge_out.get("strategy_type", "none")
    est_loss = None
    margin_required = None
    if strategy in risk_guard.SHORT_PREMIUM_STRATEGIES:
        atm = [c for c in cands if c.target_delta == 0.50]
        ce = next((c for c in atm if c.instrument_type == "CE"), None)
        pe = next((c for c in atm if c.instrument_type == "PE"), None)
        if ce is not None and pe is not None:
            est_loss = (ce.ltp + pe.ltp) * ce.lot_units * _SL_MULTIPLE
            if strategy == "iron_fly":
                capped = _iron_fly_max_loss(cands, ce, pe)
                if capped is not None:
                    est_loss = capped
            margin_required = _margin_required_basket(kite, commodity, ce, pe)

    margin_available = _margin_available(kite, commodity)
    verdict = risk_guard.evaluate(
        risk_guard.RiskLimits(
            max_loss_per_lot=settings.COMMODITY_MAX_LOSS_PER_LOT,
            max_daily_loss=settings.MAX_DAILY_LOSS_ABS,
            max_concurrent_commodities=settings.COMMODITY_MAX_CONCURRENT,
            max_margin_util_pct=settings.COMMODITY_MAX_MARGIN_UTIL_PCT,
            max_lots=settings.COMMODITY_AGENT_MAX_LOTS,
        ),
        risk_guard.RiskInput(
            commodity=commodity,
            strategy_type=strategy,
            lots=1,   # advisory sizing; actual lots decided at approval time
            est_max_loss_per_lot=est_loss,
            daily_realized_pnl=_daily_realized_pnl(session, now),
            open_commodity_count=_open_commodity_count(session),
            margin_available=margin_available,
            margin_required=margin_required,
            blackout=blackout,
            open_group_shorts=_open_group_shorts(session, commodity),
            group_name=group_for(commodity),
        ),
    )
    run.risk_json = json.dumps(verdict.to_dict())

    vetoed = not verdict.approved
    _persist_recommendation(
        session, run, commodity, now,
        direction="NO_TRADE" if vetoed else judge_out.get("direction", "NO_TRADE"),
        strategy_type="none" if vetoed else strategy,
        strikes_syms=judge_out.get("strikes", []),
        confidence=judge_out.get("confidence"),
        reasoning=(
            ("RISK GUARD VETO: " + "; ".join(verdict.reasons) + " | Judge said: ")
            if vetoed else ""
        ) + str(judge_out.get("reasoning_summary", "")),
        dissent=judge_out.get("dissenting_views", []),
        risk_vetoed=vetoed, settings=settings,
        suggested_lots=None if vetoed else _suggested_lots(
            settings, est_loss, margin_required, margin_available),
    )
    run.status = "COMPLETED"


def _regime_flip_alert(session: Any, settings: Any, run: AgentRun,
                       commodity: str, new_label: str) -> None:
    if new_label not in (TRENDING, HIGH_VOL_EXPANSION):
        return
    prev = (
        session.query(AgentRun)
        .filter(AgentRun.commodity == commodity,
                AgentRun.id != run.id,
                AgentRun.regime_json != None)  # noqa: E711
        .order_by(AgentRun.started_at.desc())
        .first()
    )
    if prev is None:
        return
    prev_label = (json.loads(prev.regime_json or "{}") or {}).get("label")
    if prev_label == new_label or prev_label is None:
        return
    # only alert when there is actually a short option book to protect
    from app.storage import Order
    open_shorts = (
        session.query(Position)
        .join(Order, Position.order_id == Order.id)
        .filter(Position.underlying == commodity,
                Position.instrument_type.in_(["CE", "PE"]),
                Order.transaction_type == "SELL")
        .count()
    )
    if not open_shorts:
        return
    from app.commodity_agents.notify import send_telegram
    send_telegram(settings, (
        f"⚠️ <b>REGIME FLIP — {commodity}</b>\n"
        f"{prev_label} → <b>{new_label}</b> with {open_shorts} open short option "
        f"leg(s).\nThe short-premium thesis may no longer hold — review the "
        f"position now instead of waiting for the stop."
    ))
    log.info("[commodity_agents] regime flip alert %s: %s -> %s",
             commodity, prev_label, new_label)


def _suggested_lots(settings: Any, est_loss_per_lot: float | None,
                    margin_required: float | None,
                    margin_available: float | None) -> int | None:
    """Deterministic size hint: how many lots the daily loss budget and margin
    actually support. 0 is a legitimate answer (worst case exceeds budget)."""
    if not est_loss_per_lot or est_loss_per_lot <= 0:
        return None
    lots = int(settings.MAX_DAILY_LOSS_ABS // est_loss_per_lot)
    if margin_required and margin_available and margin_required > 0:
        margin_cap = settings.COMMODITY_MAX_MARGIN_UTIL_PCT / 100.0
        lots = min(lots, int(margin_available * margin_cap // margin_required))
    return max(0, min(lots, settings.COMMODITY_AGENT_MAX_LOTS))


def _iron_fly_max_loss(cands: list, atm_ce: Any, atm_pe: Any) -> float | None:
    """Defined-risk cap: widest wing distance minus net credit, per lot.
    None when 25-delta wings are missing (caller falls back to the naked
    straddle estimate, which is strictly larger — conservative)."""
    wings = [c for c in cands if c.target_delta == 0.25]
    wing_ce = next((c for c in wings if c.instrument_type == "CE"), None)
    wing_pe = next((c for c in wings if c.instrument_type == "PE"), None)
    if wing_ce is None or wing_pe is None:
        return None
    width_up = wing_ce.strike - atm_ce.strike
    width_down = atm_pe.strike - wing_pe.strike
    if width_up <= 0 or width_down <= 0:
        return None
    net_credit = (atm_ce.ltp + atm_pe.ltp) - (wing_ce.ltp + wing_pe.ltp)
    return max(max(width_up, width_down) - net_credit, 0.0) * atm_ce.lot_units


def llm_stage(run: AgentRun, session: Any, field: str, fn: Any) -> dict:
    try:
        out = fn()
    except LlmError as exc:
        setattr(run, field, json.dumps({"error": str(exc)}))
        session.commit()
        raise
    setattr(run, field, json.dumps(out))
    session.commit()
    return out


def _persist_recommendation(
    session: Any, run: AgentRun, commodity: str, now: datetime,
    direction: str, strategy_type: str, strikes_syms: list,
    confidence: float | None, reasoning: str, dissent: list, risk_vetoed: bool,
    settings: Any = None, suggested_lots: int | None = None,
) -> None:
    rec = CommodityRecommendation(
        run_pk=run.id,
        commodity=commodity,
        created_at=now,
        direction=direction,
        strategy_type=strategy_type,
        strikes_json=json.dumps(strikes_syms),
        confidence=confidence,
        reasoning_summary=reasoning,
        dissent_json=json.dumps(dissent),
        risk_vetoed=risk_vetoed,
        status="PROPOSED",
        suggested_lots=suggested_lots,
    )
    session.add(rec)
    session.commit()

    if settings is not None and not risk_vetoed:
        from app.commodity_agents.notify import notify_recommendation
        notify_recommendation(settings, {
            "id": rec.id, "commodity": commodity, "direction": direction,
            "strategy_type": strategy_type, "strikes": strikes_syms,
            "confidence": confidence, "reasoning_summary": reasoning,
        })


# ── Entry points for scheduler / routes ──────────────────────────────────────

def run_all_commodities(session_factory: Any, settings: Any) -> None:
    from app.kite_session import get_session_manager
    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[commodity_agents] no Kite session, skipping cycle: %s", exc)
        return
    llm = _make_llm(settings)
    now = datetime.now(IST)
    for commodity in settings.COMMODITY_AGENTS_COMMODITIES:
        if not market_open(commodity, now):
            log.debug("[commodity_agents] %s market closed, skipping", commodity)
            continue
        run_commodity_pipeline(commodity, session_factory, settings, kite, llm)


def _make_llm(settings: Any) -> LlmClient | None:
    if not settings.ANTHROPIC_API_KEY:
        return None
    return LlmClient(
        api_key=settings.ANTHROPIC_API_KEY,
        role_models={
            "trend": settings.COMMODITY_AGENT_MODEL_TREND,
            "event": settings.COMMODITY_AGENT_MODEL_EVENT,
            "vol": settings.COMMODITY_AGENT_MODEL_VOL,
            "judge": settings.COMMODITY_AGENT_MODEL_JUDGE,
        },
        enable_web_search=settings.COMMODITY_AGENTS_WEB_SEARCH,
    )


def register_jobs(scheduler: Any, settings: Any, session_factory: Any) -> None:
    """Called from make_scheduler() when COMMODITY_AGENTS_ENABLED=true.

    - regular cadence during MCX hours (09:00–23:30 IST)
    - off-cycle runs a couple of minutes after the weekly EIA releases so a
      fresh read lands right after the number, not at the next 30-min tick
    """
    interval = settings.COMMODITY_AGENTS_INTERVAL_MIN
    # Cron minute fields only span 0-59, so intervals of an hour or more are
    # expressed as an hour step at minute 0 (e.g. 120 -> hour="9-23/2").
    if interval >= 60:
        cadence = {"hour": f"9-23/{max(1, interval // 60)}", "minute": "0"}
    else:
        cadence = {"hour": "9-23", "minute": f"*/{interval}"}
    scheduler.add_job(
        run_all_commodities,
        trigger="cron",
        args=[session_factory, settings],
        id="commodity_agents_cycle",
        misfire_grace_time=300,
        **cadence,
    )
    scheduler.add_job(
        run_commodity_pipeline_by_name, trigger="cron", day_of_week="thu",
        hour=20, minute=5, args=["NATURALGAS", session_factory, settings],
        id="commodity_agents_eia_ng", misfire_grace_time=300,
    )
    scheduler.add_job(
        run_commodity_pipeline_by_name, trigger="cron", day_of_week="wed",
        hour=20, minute=5, args=["CRUDEOIL", session_factory, settings],
        id="commodity_agents_eia_crude", misfire_grace_time=300,
    )

    # Decision-support periphery: greeks watch, journal, calibration, briefing,
    # calendar verification. All read-only or Telegram-only — no order placement.
    from app.commodity_agents.briefing import briefing_job
    from app.commodity_agents.calendar_refresh import calendar_refresh_job
    from app.commodity_agents.calibration import calibration_job
    from app.commodity_agents.journal import sync_job
    from app.commodity_agents.portfolio import check_delta_drift
    scheduler.add_job(
        check_delta_drift, trigger="cron", hour="9-23", minute="*/15",
        args=[session_factory, settings],
        id="commodity_agents_delta_drift", misfire_grace_time=120,
    )
    scheduler.add_job(
        sync_job, trigger="cron", hour="9-23", minute=20,
        args=[session_factory],
        id="commodity_agents_journal_sync", misfire_grace_time=300,
    )
    scheduler.add_job(
        calibration_job, trigger="cron", hour=8, minute=50,
        args=[session_factory],
        id="commodity_agents_calibration", misfire_grace_time=600,
    )
    scheduler.add_job(
        briefing_job, trigger="cron", hour=8, minute=45,
        args=[session_factory, settings],
        id="commodity_agents_briefing", misfire_grace_time=600,
    )
    scheduler.add_job(
        calendar_refresh_job, trigger="cron", day_of_week="sun", hour=10, minute=0,
        args=[settings],
        id="commodity_agents_calendar_refresh", misfire_grace_time=3600,
    )
    from app.commodity_agents.weekly_report import weekly_report_job
    scheduler.add_job(
        weekly_report_job, trigger="cron", day_of_week="sun", hour=18, minute=0,
        args=[session_factory, settings],
        id="commodity_agents_weekly_report", misfire_grace_time=3600,
    )
    log.info("[commodity_agents] jobs registered: pipeline every %d min 09-23 IST, "
             "EIA off-cycle, delta watch, journal, calibration, briefing, "
             "calendar refresh", interval)


def run_commodity_pipeline_by_name(commodity: str, session_factory: Any, settings: Any) -> None:
    from app.kite_session import get_session_manager
    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[commodity_agents] no Kite session for %s run: %s", commodity, exc)
        return
    run_commodity_pipeline(commodity, session_factory, settings, kite, _make_llm(settings))
