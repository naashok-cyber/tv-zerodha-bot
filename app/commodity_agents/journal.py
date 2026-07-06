"""Desk trade journal — the feedback loop that separates survivors.

Every human APPROVE writes a journal row with the entry context frozen at
that moment (regime label, VRP, IV trend, judge confidence, edge ratio).
For live trades, a sync pass later joins the row to ClosedTrade P&L via
Alert → Order → Position → ClosedTrade. Expectancy stats then answer the
only question that matters after 50 trades: which setups actually pay?
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.commodity_agents.storage import AgentRun, CommodityTradeJournal
from app.config import IST

log = logging.getLogger(__name__)


def build_entry_context(run: AgentRun, confidence: float | None) -> dict:
    regime = json.loads(run.regime_json or "{}")
    quant = json.loads(run.analytics_json or "{}")
    events = json.loads(run.events_json or "[]")
    return {
        "regime": regime.get("label"),
        "adx": regime.get("adx"),
        "iv_percentile": regime.get("iv_percentile"),
        "vrp_pts": (quant.get("vrp") or {}).get("vrp_pts"),
        "iv_trend": (quant.get("iv_trend") or {}).get("direction"),
        "edge_ratio": (quant.get("expected_move") or {}).get("edge_ratio"),
        "judge_confidence": confidence,
        "events_in_horizon": len(events),
    }


def record_entry(session: Any, rec: Any, run: AgentRun, mode: str,
                 lots: int, alert_id: int | None, now: datetime | None = None) -> None:
    session.add(CommodityTradeJournal(
        recommendation_id=rec.id,
        commodity=rec.commodity,
        mode=mode,
        alert_id=alert_id,
        entered_at=now or datetime.now(IST),
        entry_context_json=json.dumps(build_entry_context(run, rec.confidence)),
        lots=lots,
    ))
    session.commit()


def sync_closed_trades(session_factory: Any) -> int:
    """Back-fill realized P&L, slippage, and MAE/MFE on live journal rows whose
    legs have all closed. Returns the number of rows updated. Safe to run any time."""
    from app.commodity_agents.storage import CommodityRecommendation
    from app.storage import ClosedTrade, Order, Position

    updated = 0
    with session_factory() as session:
        pending = (
            session.query(CommodityTradeJournal)
            .filter(CommodityTradeJournal.mode == "live",
                    CommodityTradeJournal.realized_pnl == None,  # noqa: E711
                    CommodityTradeJournal.alert_id != None)      # noqa: E711
            .all()
        )
        for row in pending:
            orders = session.query(Order).filter(Order.alert_id == row.alert_id).all()
            if not orders:
                continue
            legs: list[tuple[Any, Any, Any]] = []      # (order, position, closed_trade)
            for order in orders:
                pos = session.query(Position).filter(Position.order_id == order.id).first()
                if pos is None:
                    break
                ct = (session.query(ClosedTrade)
                      .filter(ClosedTrade.position_id == pos.id).first())
                if ct is None:
                    break
                legs.append((order, pos, ct))
            else:
                if legs:
                    row.realized_pnl = sum(ct.pnl for _, _, ct in legs)
                    row.closed_at = max(ct.closed_at for _, _, ct in legs)
                    row.exit_reason = legs[0][2].exit_reason
                    row.slippage_pct = _entry_slippage_pct(session, row, legs)
                    row.mae_pct, row.mfe_pct = _excursions_pct(legs)
                    updated += 1
        if updated:
            session.commit()
            log.info("[journal] synced %d closed trade(s)", updated)
    return updated


def _entry_slippage_pct(session: Any, row: CommodityTradeJournal,
                        legs: list) -> float | None:
    """Fill price vs the analysis-time quote, premium-weighted across legs.
    Negative = you got a worse price than the recommendation assumed."""
    from app.commodity_agents.storage import CommodityRecommendation
    rec = session.get(CommodityRecommendation, row.recommendation_id)
    run = session.get(AgentRun, rec.run_pk) if rec else None
    if run is None or not run.strikes_json:
        return None
    quotes = {c["tradingsymbol"]: c["ltp"]
              for c in json.loads(run.strikes_json) if c.get("ltp")}
    num = 0.0
    den = 0.0
    for order, _, _ in legs:
        quote = quotes.get(order.tradingsymbol)
        fill = order.fill_price
        if not quote or not fill:
            continue
        # short: filling below the quoted premium is adverse; long: above is
        sign = 1.0 if order.transaction_type == "SELL" else -1.0
        num += sign * (fill - quote) / quote * 100.0 * quote
        den += quote
    return round(num / den, 3) if den else None


def _excursions_pct(legs: list) -> tuple[float | None, float | None]:
    """Worst adverse / best favorable per-leg excursion as % of entry premium.
    Per-leg extremes are not simultaneous — this bounds, not reconstructs, the
    combined path. None until the trailing manager has recorded extremes."""
    mae = None
    mfe = None
    for order, pos, _ in legs:
        entry = pos.entry_premium
        if not entry:
            continue
        short = order.transaction_type == "SELL"
        if pos.max_adverse_price is not None:
            adverse = ((pos.max_adverse_price - entry) if short
                       else (entry - pos.max_adverse_price)) / entry * 100.0
            mae = max(mae, adverse) if mae is not None else adverse
        if pos.max_favorable_price is not None:
            favorable = ((entry - pos.max_favorable_price) if short
                         else (pos.max_favorable_price - entry)) / entry * 100.0
            mfe = max(mfe, favorable) if mfe is not None else favorable
    return (round(mae, 2) if mae is not None else None,
            round(mfe, 2) if mfe is not None else None)


def expectancy_stats(session: Any) -> dict:
    """Win rate / mean P&L per regime label, live closed trades only."""
    rows = (
        session.query(CommodityTradeJournal)
        .filter(CommodityTradeJournal.realized_pnl != None)  # noqa: E711
        .all()
    )
    by_regime: dict[str, list[float]] = {}
    for row in rows:
        ctx = json.loads(row.entry_context_json or "{}")
        by_regime.setdefault(ctx.get("regime") or "unknown", []).append(row.realized_pnl)
    out = {}
    for label, pnls in by_regime.items():
        wins = [p for p in pnls if p > 0]
        out[label] = {
            "trades": len(pnls),
            "win_rate_pct": round(100.0 * len(wins) / len(pnls), 1),
            "mean_pnl": round(sum(pnls) / len(pnls)),
            "total_pnl": round(sum(pnls)),
            "worst": round(min(pnls)),
        }
    return out


def sync_job(session_factory: Any) -> None:
    """Scheduler entry point — never raises."""
    try:
        sync_closed_trades(session_factory)
    except Exception as exc:
        log.error("[journal] sync failed: %s", exc)
