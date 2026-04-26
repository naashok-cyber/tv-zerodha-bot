from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from cachetools import TTLCache
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import HMACError, IPBlockedError, RateLimitError, check_ip, check_rate_limit, verify_hmac
from app.config import IST, Settings, get_settings
from app.kite_session import get_session_manager
from app.storage import Alert, init_db
from app.symbol_mapper import resolve_underlying
from app.watcher import OrderWatcher
from app.webhook_models import Action, TradingViewAlert

log = logging.getLogger(__name__)

# ── Module-level singletons ────────────────────────────────────────────────────
# _SessionFactory is set in lifespan; tests inject a factory via monkeypatch before
# the lifespan runs so the guard `if _SessionFactory is None` skips real DB init.
_SessionFactory: Any = None
_watcher: OrderWatcher | None = None
_idempotency_cache: TTLCache = TTLCache(maxsize=10_000, ttl=86_400)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SessionFactory, _watcher
    if _SessionFactory is None:
        from sqlalchemy.orm import sessionmaker
        engine = init_db(get_settings().DATABASE_URL)
        _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
    _watcher = OrderWatcher()
    yield


app = FastAPI(title="tv-zerodha-bot", version="0.1.0", lifespan=lifespan)
_security = HTTPBasic(auto_error=False)


# ── Dependencies ──────────────────────────────────────────────────────────────

def get_current_settings() -> Settings:
    return get_settings()


def get_db_session() -> Any:
    if _SessionFactory is None:
        raise RuntimeError("Database not initialized — lifespan must run first")
    with _SessionFactory() as session:
        yield session


def _auth_guard(
    credentials: HTTPBasicCredentials | None = Depends(_security),
    settings: Settings = Depends(get_current_settings),
) -> None:
    if not settings.DASHBOARD_PASSWORD:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok = secrets.compare_digest(
        credentials.username, settings.DASHBOARD_USERNAME
    ) and secrets.compare_digest(credentials.password, settings.DASHBOARD_PASSWORD)
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _build_alert_row(alert_data: TradingViewAlert, raw: str, ikey: str) -> Alert:
    return Alert(
        received_at=datetime.now(IST),
        strategy_id=alert_data.strategy_id,
        tv_ticker=alert_data.tv_ticker,
        tv_exchange=alert_data.tv_exchange,
        action=alert_data.action.value,
        order_type=alert_data.order_type.value if alert_data.order_type else None,
        entry_price=alert_data.entry_price,
        stop_loss=alert_data.stop_loss,
        sl_percent=alert_data.sl_percent,
        atr=alert_data.atr,
        quantity_hint=alert_data.quantity_hint,
        product=alert_data.product,
        tv_time=alert_data.tv_time,
        bar_time=alert_data.bar_time,
        interval=alert_data.interval,
        idempotency_key=ikey,
        raw_payload=raw,
        processed=False,
    )


# ── Background task ───────────────────────────────────────────────────────────

def _process_alert(alert_id: int, alert_data: TradingViewAlert, settings: Settings) -> None:
    with _SessionFactory() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            log.error("Alert %d not found in DB (background task)", alert_id)
            return

        underlying = resolve_underlying(alert_data.tv_ticker, settings=settings)

        if underlying.is_natural_gas:
            log.info("Alert %d: NG future path not wired in P0-e, deferred to P1", alert_id)
            alert.processed = True
            session.commit()
            return

        if alert_data.action in (Action.EXIT, Action.TRAIL):
            log.info("Alert %d: EXIT/TRAIL path not wired in P0-e, deferred to P1", alert_id)
            alert.processed = True
            session.commit()
            return

        if settings.DRY_RUN:
            log.info(
                "Alert %d: DRY_RUN — would place %s %s on %s (segment=%s)",
                alert_id,
                alert_data.action.value,
                alert_data.tv_ticker,
                underlying.name,
                underlying.segment,
            )
            alert.processed = True
            session.commit()
            return

        # Non-NG options entry — pipeline not yet wired
        log.info("Alert %d: options pipeline not wired in P0-e, deferred to P1", alert_id)
        alert.processed = True
        session.commit()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_current_settings),
    session: Session = Depends(get_db_session),
) -> dict:
    client_ip = _get_client_ip(request)

    try:
        check_ip(client_ip, allowed_ips=settings.TV_ALLOWED_IPS)
    except IPBlockedError:
        raise HTTPException(status_code=401, detail="IP not in allowlist")

    try:
        check_rate_limit(client_ip)
    except RateLimitError:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    try:
        raw_bytes = await request.body()
        raw_str = raw_bytes.decode()
        body = json.loads(raw_str)
        alert_data = TradingViewAlert.model_validate(body)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        verify_hmac(alert_data.secret, expected_secret=settings.WEBHOOK_SECRET)
    except HMACError:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    ikey = alert_data.idempotency_key()
    if ikey in _idempotency_cache:
        return {"status": "duplicate"}
    _idempotency_cache[ikey] = True

    alert_row = _build_alert_row(alert_data, raw_str, ikey)
    session.add(alert_row)
    session.commit()
    session.refresh(alert_row)

    background_tasks.add_task(_process_alert, alert_row.id, alert_data, settings)

    return {"status": "queued", "alert_id": alert_row.id}


@app.get("/kite/callback")
async def kite_callback(status: str = "unknown", request_token: str = "") -> Response:
    if status != "success" or not request_token:
        raise HTTPException(
            status_code=400,
            detail=f"Kite callback failed: status={status!r}",
        )
    try:
        get_session_manager().handle_callback(request_token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {exc}")
    return Response(
        content="<html><body><h2>Login complete. You may close this window.</h2></body></html>",
        media_type="text/html",
    )


@app.get("/healthz")
async def healthz(settings: Settings = Depends(get_current_settings)) -> dict:
    token_age_hours = None
    try:
        result = get_session_manager()._load_token()
        if result is not None:
            _, created_at = result
            age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
            token_age_hours = round(age.total_seconds() / 3600, 2)
    except Exception:
        pass
    return {"status": "ok", "token_age_hours": token_age_hours, "dry_run": settings.DRY_RUN}


@app.get("/dashboard")
async def dashboard(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
) -> Response:
    rows = session.query(Alert).order_by(Alert.received_at.desc()).limit(50).all()
    rows_html = "".join(
        f"<tr><td>{r.id}</td><td>{r.tv_ticker}</td><td>{r.action}</td>"
        f"<td>{r.received_at}</td><td>{r.processed}</td></tr>"
        for r in rows
    )
    return Response(
        content=(
            "<html><body><h2>Recent Alerts</h2>"
            "<table border='1'><tr><th>ID</th><th>Ticker</th><th>Action</th>"
            f"<th>Time</th><th>Processed</th></tr>{rows_html}</table></body></html>"
        ),
        media_type="text/html",
    )


@app.get("/positions")
async def positions(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
) -> Response:
    from app.storage import Position
    rows = session.query(Position).all()
    rows_html = "".join(
        f"<tr><td>{r.id}</td><td>{r.tradingsymbol}</td>"
        f"<td>{r.quantity}</td><td>{r.entry_premium}</td></tr>"
        for r in rows
    )
    return Response(
        content=(
            "<html><body><h2>Open Positions</h2>"
            "<table border='1'><tr><th>ID</th><th>Symbol</th>"
            f"<th>Qty</th><th>Entry Premium</th></tr>{rows_html}</table></body></html>"
        ),
        media_type="text/html",
    )


@app.get("/history")
async def history(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
) -> Response:
    from app.storage import ClosedTrade
    rows = session.query(ClosedTrade).order_by(ClosedTrade.closed_at.desc()).limit(50).all()
    rows_html = "".join(
        f"<tr><td>{r.id}</td><td>{r.tradingsymbol}</td>"
        f"<td>{r.pnl}</td><td>{r.exit_reason}</td><td>{r.closed_at}</td></tr>"
        for r in rows
    )
    return Response(
        content=(
            "<html><body><h2>Trade History</h2>"
            "<table border='1'><tr><th>ID</th><th>Symbol</th><th>PnL</th>"
            f"<th>Reason</th><th>Closed</th></tr>{rows_html}</table></body></html>"
        ),
        media_type="text/html",
    )


@app.get("/status")
async def status_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    from app.storage import AppError
    errors = session.query(AppError).order_by(AppError.occurred_at.desc()).limit(10).all()
    errors_html = "".join(
        f"<tr><td>{e.id}</td><td>{e.error_type}</td><td>{e.message[:80]}</td></tr>"
        for e in errors
    )
    return Response(
        content=(
            f"<html><body><h2>Bot Status</h2>"
            f"<p>DRY_RUN: {settings.DRY_RUN} | TRADING_ENABLED: {settings.TRADING_ENABLED}</p>"
            f"<h3>Recent Errors</h3><table border='1'><tr><th>ID</th><th>Type</th>"
            f"<th>Message</th></tr>{errors_html}</table></body></html>"
        ),
        media_type="text/html",
    )
