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
    """Back-fill realized P&L on live journal rows whose legs have all closed.
    Returns the number of rows updated. Safe to run any time."""
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
            closed: list[ClosedTrade] = []
            for order in orders:
                pos = session.query(Position).filter(Position.order_id == order.id).first()
                if pos is None:
                    break
                ct = (session.query(ClosedTrade)
                      .filter(ClosedTrade.position_id == pos.id).first())
                if ct is None:
                    break
                closed.append(ct)
            else:
                if closed:
                    row.realized_pnl = sum(c.pnl for c in closed)
                    row.closed_at = max(c.closed_at for c in closed)
                    row.exit_reason = closed[0].exit_reason
                    updated += 1
        if updated:
            session.commit()
            log.info("[journal] synced %d closed trade(s)", updated)
    return updated


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
