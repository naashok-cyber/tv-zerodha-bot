"""REST API for the commodity debate agents.

Auth: X-Admin-Auth-Token header (same token as /admin/voice — one admin
credential, constant-time compared).

Approval is deliberately TWO-step so a fast double-tap on a phone cannot fire
an execution: step 1 returns a short-lived confirm token, step 2 must echo it
back. Even a confirmed APPROVE only records the decision — live order
placement is Phase 6, gated behind COMMODITY_AGENTS_LIVE and not implemented.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from app.commodity_agents.models import COMMODITIES
from app.commodity_agents.storage import (
    AgentRun,
    CommodityRecommendation,
    RecommendationDecision,
)
from app.config import IST, get_settings
from app.storage import get_db_session  # noqa: F401  (factory registered by main)
from app import storage as app_storage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/commodity-agents", tags=["commodity-agents"])

_CONFIRM_TTL_SECONDS = 60
_RUN_COOLDOWN_SECONDS = 60          # on-demand trigger rate limit (LLM cost)
_confirm_tokens: dict[int, tuple[str, float]] = {}   # rec_id -> (token, expiry)
_last_manual_run: dict[str, float] = {}              # per-commodity cooldown
_lock = threading.Lock()


def _require_admin(x_admin_auth_token: str | None) -> None:
    expected = get_settings().ADMIN_AUTH_TOKEN
    if not expected or not x_admin_auth_token or \
            not hmac.compare_digest(expected, x_admin_auth_token):
        raise HTTPException(status_code=401, detail="unauthorized")


def _session():
    if app_storage._factory is None:
        raise HTTPException(status_code=503, detail="db not ready")
    return app_storage._factory()


def _rec_to_dict(rec: CommodityRecommendation) -> dict:
    return {
        "id": rec.id,
        "run_id": rec.run.run_id,
        "commodity": rec.commodity,
        "created_at": rec.created_at.isoformat(),
        "direction": rec.direction,
        "strategy_type": rec.strategy_type,
        "strikes": json.loads(rec.strikes_json or "[]"),
        "confidence": rec.confidence,
        "reasoning_summary": rec.reasoning_summary,
        "dissenting_views": json.loads(rec.dissent_json or "[]"),
        "risk_vetoed": rec.risk_vetoed,
        "status": rec.status,
    }


@router.get("/recommendations")
def latest_recommendations(x_admin_auth_token: str | None = Header(default=None)) -> dict:
    _require_admin(x_admin_auth_token)
    out = {}
    with _session() as session:
        for commodity in COMMODITIES:
            rec = (
                session.query(CommodityRecommendation)
                .filter(CommodityRecommendation.commodity == commodity)
                .order_by(CommodityRecommendation.created_at.desc())
                .first()
            )
            out[commodity] = _rec_to_dict(rec) if rec else None
    return {"recommendations": out}


@router.get("/{commodity}/history")
def history(
    commodity: str,
    limit: int = 50,
    x_admin_auth_token: str | None = Header(default=None),
) -> dict:
    _require_admin(x_admin_auth_token)
    commodity = commodity.upper()
    if commodity not in COMMODITIES:
        raise HTTPException(status_code=404, detail=f"unknown commodity {commodity}")
    with _session() as session:
        recs = (
            session.query(CommodityRecommendation)
            .filter(CommodityRecommendation.commodity == commodity)
            .order_by(CommodityRecommendation.created_at.desc())
            .limit(min(limit, 200))
            .all()
        )
        return {"commodity": commodity, "history": [_rec_to_dict(r) for r in recs]}


@router.get("/runs/{run_id}")
def run_detail(run_id: str, x_admin_auth_token: str | None = Header(default=None)) -> dict:
    """Full reasoning trail for one pipeline run — the drill-down view."""
    _require_admin(x_admin_auth_token)
    with _session() as session:
        run = session.query(AgentRun).filter(AgentRun.run_id == run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run_id": run.run_id,
            "commodity": run.commodity,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "status": run.status,
            "regime": json.loads(run.regime_json or "null"),
            "events": json.loads(run.events_json or "null"),
            "strikes": json.loads(run.strikes_json or "null"),
            "analytics": json.loads(run.analytics_json or "null"),
            "trend_agent": json.loads(run.trend_json or "null"),
            "event_agent": json.loads(run.event_debate_json or "null"),
            "vol_agent": json.loads(run.vol_json or "null"),
            "judge": json.loads(run.judge_json or "null"),
            "risk_guard": json.loads(run.risk_json or "null"),
            "error": run.error,
        }


# ── Dashboard / PWA assets (unauthenticated shell; data calls carry token) ──

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> str:
    from app.commodity_agents.dashboard import DASHBOARD_HTML
    return DASHBOARD_HTML


@router.get("/analyze", response_class=HTMLResponse)
def analyze_page() -> str:
    from app.commodity_agents.dashboard import ANALYZE_HTML
    return ANALYZE_HTML


@router.get("/manifest.json")
def manifest() -> JSONResponse:
    from app.commodity_agents.dashboard import MANIFEST_JSON
    return JSONResponse(MANIFEST_JSON)


@router.get("/sw.js")
def service_worker() -> Response:
    from app.commodity_agents.dashboard import SERVICE_WORKER_JS
    return Response(SERVICE_WORKER_JS, media_type="application/javascript")


class RunRequest(BaseModel):
    commodity: str | None = None    # None = all four


@router.post("/run")
def trigger_run(
    body: RunRequest,
    background: BackgroundTasks,
    x_admin_auth_token: str | None = Header(default=None),
) -> dict:
    _require_admin(x_admin_auth_token)
    commodity = body.commodity.upper() if body.commodity else "ALL"
    if commodity != "ALL" and commodity not in COMMODITIES:
        raise HTTPException(status_code=404, detail=f"unknown commodity {commodity}")
    with _lock:
        now = time.monotonic()
        if now - _last_manual_run.get(commodity, 0.0) < _RUN_COOLDOWN_SECONDS:
            raise HTTPException(status_code=429,
                                detail=f"{commodity} run already triggered recently")
        _last_manual_run[commodity] = now

    from app.commodity_agents.orchestrator import (
        run_all_commodities,
        run_commodity_pipeline_by_name,
    )
    settings = get_settings()
    factory = app_storage._factory
    if commodity != "ALL":
        background.add_task(run_commodity_pipeline_by_name, commodity, factory, settings)
    else:
        background.add_task(run_all_commodities, factory, settings)
    return {"status": "started", "commodity": commodity}


# Pipeline stages in execution order; each maps to the AgentRun column that is
# filled when the stage completes — this is what drives the live progress UI.
_STAGES = [
    ("events", "events_json", "Event calendar"),
    ("strikes", "strikes_json", "Strike candidates (Black-76/BSM)"),
    ("regime", "regime_json", "Regime classifier"),
    ("analytics", "analytics_json", "Vol analytics (VRP / IV trend / stress)"),
    ("trend", "trend_json", "Trend debate agent"),
    ("gap_risk", "event_debate_json", "Event/Gap-risk agent (news search)"),
    ("vol", "vol_json", "Volatility debate agent"),
    ("judge", "judge_json", "Judge synthesis"),
    ("risk_guard", "risk_json", "Risk Guard"),
]


@router.get("/{commodity}/runs/latest")
def latest_run(
    commodity: str,
    x_admin_auth_token: str | None = Header(default=None),
) -> dict:
    """Most recent pipeline run for a ticker, with per-stage completion —
    polled by the Analyze page while a run is in flight."""
    _require_admin(x_admin_auth_token)
    commodity = commodity.upper()
    if commodity not in COMMODITIES:
        raise HTTPException(status_code=404, detail=f"unknown commodity {commodity}")
    with _session() as session:
        run = (
            session.query(AgentRun)
            .filter(AgentRun.commodity == commodity)
            .order_by(AgentRun.started_at.desc())
            .first()
        )
        if run is None:
            return {"run": None}
        rec = (
            session.query(CommodityRecommendation)
            .filter(CommodityRecommendation.run_pk == run.id)
            .first()
        )
        return {"run": {
            "run_id": run.run_id,
            "commodity": run.commodity,
            "started_at": run.started_at.isoformat(),
            "status": run.status,
            "error": run.error,
            "stages": [
                {"key": key, "label": label,
                 "done": bool(getattr(run, field))}
                for key, field, label in _STAGES
            ],
            "analytics": json.loads(run.analytics_json or "null"),
            "recommendation": _rec_to_dict(rec) if rec else None,
        }}


@router.get("/{commodity}/iv-history")
def iv_history(
    commodity: str,
    limit: int = 200,
    x_admin_auth_token: str | None = Header(default=None),
) -> dict:
    """ATM IV + realized vol time series across pipeline runs — powers the
    IV-over-time chart on the Analyze page."""
    _require_admin(x_admin_auth_token)
    commodity = commodity.upper()
    if commodity not in COMMODITIES:
        raise HTTPException(status_code=404, detail=f"unknown commodity {commodity}")
    with _session() as session:
        runs = (
            session.query(AgentRun)
            .filter(AgentRun.commodity == commodity, AgentRun.atm_iv != None)  # noqa: E711
            .order_by(AgentRun.started_at.desc())
            .limit(min(limit, 500))
            .all()
        )
        points = []
        trend = None
        for run in reversed(runs):     # oldest → newest for plotting
            rv = None
            if run.regime_json:
                try:
                    rv = json.loads(run.regime_json).get("realized_vol")
                except (ValueError, AttributeError):
                    pass
            points.append({
                "t": run.started_at.isoformat(),
                "iv": run.atm_iv,
                "rv": rv,
                "vrp": round(run.atm_iv - rv, 4) if rv is not None else None,
            })
        if runs and runs[0].analytics_json:
            try:
                trend = json.loads(runs[0].analytics_json).get("iv_trend")
            except ValueError:
                pass
        return {"commodity": commodity, "points": points, "iv_trend": trend}


@router.get("/journal")
def journal(x_admin_auth_token: str | None = Header(default=None)) -> dict:
    """Desk journal: every approved trade with its frozen entry context,
    plus expectancy stats grouped by regime."""
    _require_admin(x_admin_auth_token)
    from app.commodity_agents.journal import expectancy_stats, sync_closed_trades
    sync_closed_trades(app_storage._factory)     # opportunistic back-fill
    from app.commodity_agents.storage import CommodityTradeJournal
    with _session() as session:
        rows = (
            session.query(CommodityTradeJournal)
            .order_by(CommodityTradeJournal.entered_at.desc())
            .limit(200)
            .all()
        )
        return {
            "entries": [{
                "id": r.id,
                "recommendation_id": r.recommendation_id,
                "commodity": r.commodity,
                "mode": r.mode,
                "entered_at": r.entered_at.isoformat(),
                "lots": r.lots,
                "entry_context": json.loads(r.entry_context_json or "{}"),
                "realized_pnl": r.realized_pnl,
                "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                "exit_reason": r.exit_reason,
            } for r in rows],
            "expectancy_by_regime": expectancy_stats(session),
        }


@router.get("/calibration")
def calibration(x_admin_auth_token: str | None = Header(default=None)) -> dict:
    """LLM scorecard: judge hit rate and per-agent risk-flag reliability."""
    _require_admin(x_admin_auth_token)
    from app.commodity_agents.calibration import calibration_stats
    with _session() as session:
        return calibration_stats(session)


@router.get("/portfolio-greeks")
def portfolio_greeks(x_admin_auth_token: str | None = Header(default=None)) -> dict:
    """Live delta/vega/theta per open option position + per-straddle net delta."""
    _require_admin(x_admin_auth_token)
    from app.commodity_agents.portfolio import compute_portfolio_greeks
    from app.kite_session import get_session_manager
    try:
        kite = get_session_manager().get_kite()
    except Exception:
        raise HTTPException(status_code=503, detail="no Kite session")
    with _session() as session:
        return compute_portfolio_greeks(session, kite, get_settings())


class DecisionRequest(BaseModel):
    recommendation_id: int
    action: str                     # "approve" | "reject"
    confirm_token: str | None = None
    note: str | None = None
    lots: int = 1                   # used only when live execution fires


@router.post("/decision")
def decide(
    body: DecisionRequest,
    background: BackgroundTasks,
    x_admin_auth_token: str | None = Header(default=None),
) -> dict:
    _require_admin(x_admin_auth_token)
    action = body.action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="action must be approve or reject")

    with _session() as session:
        rec = session.get(CommodityRecommendation, body.recommendation_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="recommendation not found")
        if rec.status != "PROPOSED":
            raise HTTPException(status_code=409, detail=f"already {rec.status}")
        if rec.risk_vetoed and action == "approve":
            raise HTTPException(
                status_code=409,
                detail="risk-guard-vetoed recommendation cannot be approved",
            )

        # Step 1: no token yet → issue one (the UI shows a 'confirm' second tap)
        if body.confirm_token is None:
            token = secrets.token_urlsafe(16)
            with _lock:
                _confirm_tokens[rec.id] = (token, time.monotonic() + _CONFIRM_TTL_SECONDS)
            return {"status": "confirm_required", "confirm_token": token,
                    "expires_in_seconds": _CONFIRM_TTL_SECONDS}

        # Step 2: validate token
        with _lock:
            stored = _confirm_tokens.get(rec.id)
            if not stored or stored[1] < time.monotonic() or \
                    not hmac.compare_digest(stored[0], body.confirm_token):
                raise HTTPException(status_code=410, detail="confirm token invalid or expired")
            del _confirm_tokens[rec.id]

        rec.status = "APPROVED" if action == "approve" else "REJECTED"
        session.add(RecommendationDecision(
            recommendation_id=rec.id,
            action=action.upper(),
            created_at=datetime.now(IST),
            note=body.note,
        ))
        session.commit()

        executed, exec_note, exec_alert_id = _maybe_execute(session, rec, body.lots, background)
        if action == "approve":
            # Journal every APPROVE: paper approvals build the decision record,
            # live ones get P&L back-filled by the sync job once legs close.
            from app.commodity_agents.journal import record_entry
            run = session.get(AgentRun, rec.run_pk)
            record_entry(session, rec, run, mode="live" if executed else "paper",
                         lots=body.lots, alert_id=exec_alert_id)
        return {"status": rec.status, "executed": executed, "note": exec_note}


def _maybe_execute(session, rec: CommodityRecommendation, lots: int,
                   background: BackgroundTasks) -> tuple[bool, str, int | None]:
    """Execution Agent: fires ONLY on a confirmed human APPROVE, and only when
    COMMODITY_AGENTS_LIVE=true. Routes a short straddle through the existing
    _process_straddle path so GTT OCO exits and trailing SL apply unchanged.
    Returns (executed, note, alert_id) — alert_id links the journal to fills."""
    settings = get_settings()
    if rec.status != "APPROVED":
        return False, "decision recorded", None
    if not settings.COMMODITY_AGENTS_LIVE:
        return False, "decision recorded (paper mode — COMMODITY_AGENTS_LIVE off)", None
    if rec.strategy_type != "short_straddle":
        return False, (f"decision recorded; live auto-execution only supports "
                       f"short_straddle — place {rec.strategy_type} manually"), None

    strikes_syms = json.loads(rec.strikes_json or "[]")
    ce = next((s for s in strikes_syms if s.endswith("CE")), None)
    pe = next((s for s in strikes_syms if s.endswith("PE")), None)
    if not ce or not pe:
        return False, "decision recorded; CE/PE symbols missing — place manually", None

    import uuid
    from app.commodity_agents.models import exchange_for
    from app.main import _process_straddle   # late import — avoids circular dep
    from app.storage import Alert

    lots = max(1, min(lots, settings.COMMODITY_AGENT_MAX_LOTS))
    now = datetime.now(IST)
    exch = exchange_for(rec.commodity)
    entry_data = {
        "underlying": rec.commodity,
        "exchange": exch,
        "quantity": lots,
        "_straddle_ce_symbol": ce,
        "_straddle_pe_symbol": pe,
    }
    alert = Alert(
        received_at=now,
        strategy_id=f"commodity_agent_{rec.id}",
        tv_ticker=rec.commodity,
        tv_exchange=exch,
        action="STRADDLE_SHORT",
        order_type=None, entry_price=0.0, stop_loss=None, sl_percent=None,
        atr=None, quantity_hint=lots, product="NRML",
        tv_time=now.isoformat(), bar_time=None, interval="agent",
        idempotency_key=str(uuid.uuid4()),
        raw_payload=json.dumps({"recommendation_id": rec.id, "strikes": strikes_syms}),
        processed=False,
    )
    session.add(alert)
    session.commit()
    background.add_task(_process_straddle, alert.id, entry_data, settings)
    log.info("[commodity_agents] LIVE execution queued: rec %d, %s %s/%s x%d lots",
             rec.id, rec.commodity, ce, pe, lots)
    return True, f"live short straddle queued: {ce} + {pe}, {lots} lot(s)", alert.id
