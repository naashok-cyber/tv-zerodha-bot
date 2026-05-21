from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import traceback

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import floor
from typing import Any

from cachetools import TTLCache
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.risk as risk
import app.state as state
from app.auth import IPBlockedError, RateLimitError, check_ip, check_rate_limit
from app.config import IST, Settings, get_settings
from app.expiry_resolver import NoEligibleExpiryError, resolve_expiry
from app.kite_session import get_session_manager
from app.orders import cancel_gtt, modify_gtt, place_entry, place_gtt_oco, square_off
from app.scheduler import daily_session_check, get_last_checked_at, make_scheduler, refresh_instruments_job
from app.storage import Alert, AppError, ClosedTrade, Gtt, Instrument, Order, Position, init_db
from app.strike_selector import NoValidStrikeError, select_strike
from app.symbol_mapper import resolve_underlying
from app.schemas import AlertPayload
from app.watcher import EntryFilledEvent, GttFilledEvent, OrderWatcher

log = logging.getLogger(__name__)


def _dry_run(settings: Settings) -> bool:
    """Effective dry-run flag: state override (paper-mode toggle) beats .env."""
    return state.is_paper_mode(settings.DRY_RUN)


# ── Module-level singletons ────────────────────────────────────────────────────
_SessionFactory: Any = None
_watcher: OrderWatcher | None = None
_idempotency_cache: TTLCache = TTLCache(maxsize=10_000, ttl=86_400)

# Keyed by kite_order_id; carries sl/target distances for the EntryFilledEvent callback.
_pending_order_meta: dict[str, dict] = {}

_scheduler: Any = None


# ── Risk guard helpers ────────────────────────────────────────────────────────

def _realised_loss_today(session: Any) -> float:
    """Return the total realised loss (positive number) from ClosedTrade rows closed today IST."""
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(ClosedTrade.pnl)
        .filter(ClosedTrade.closed_at >= today_start, ClosedTrade.pnl < 0)
        .all()
    )
    return abs(sum(r[0] for r in rows))


def _open_position_count(session: Any) -> int:
    return (
        session.query(Position)
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(ClosedTrade.id == None)  # noqa: E711
        .count()
    )


def _trades_today_count(session: Any) -> int:
    today_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        session.query(Alert)
        .filter(
            Alert.action.in_(["BUY", "SELL"]),
            Alert.processed == True,  # noqa: E712
            Alert.received_at >= today_start,
        )
        .count()
    )


def _consecutive_losses(session: Any) -> int:
    rows = (
        session.query(ClosedTrade.exit_reason)
        .order_by(ClosedTrade.closed_at.desc())
        .all()
    )
    count = 0
    for (reason,) in rows:
        if reason == "SL_HIT":
            count += 1
        else:
            break
    return count


def _check_risk_guards(session: Any, settings: Settings) -> tuple[bool, str]:
    if state.is_emergency_stop():
        return False, "EMERGENCY STOP is active"
    loss = _realised_loss_today(session)
    effective_loss_cap = state.get_max_daily_loss(settings.MAX_DAILY_LOSS_ABS)
    if loss >= effective_loss_cap:
        return False, f"daily loss ₹{loss:.2f} >= limit ₹{effective_loss_cap:.2f}"
    trades = _trades_today_count(session)
    if trades >= settings.MAX_TRADES_PER_DAY:
        return False, f"trades today {trades} >= MAX_TRADES_PER_DAY {settings.MAX_TRADES_PER_DAY}"
    positions = _open_position_count(session)
    if positions >= settings.MAX_OPEN_POSITIONS:
        return False, f"open positions {positions} >= MAX_OPEN_POSITIONS {settings.MAX_OPEN_POSITIONS}"
    losses = _consecutive_losses(session)
    if losses >= settings.CONSECUTIVE_LOSSES_LIMIT:
        return False, f"consecutive losses {losses} >= CONSECUTIVE_LOSSES_LIMIT {settings.CONSECUTIVE_LOSSES_LIMIT}"
    return True, ""


# ── EntryFilledEvent callback ─────────────────────────────────────────────────

def _on_entry_filled(event: EntryFilledEvent) -> None:
    """Consume a fill event: persist Position, place GTT OCO."""
    if _SessionFactory is None:
        log.error("_on_entry_filled fired but _SessionFactory is None")
        return
    settings = get_settings()
    with _SessionFactory() as session:
        order = (
            session.query(Order)
            .filter(Order.kite_order_id == event.kite_order_id)
            .first()
        )
        if order is None:
            log.error("EntryFilledEvent: no Order row for kite_order_id=%s", event.kite_order_id)
            return

        now = datetime.now(IST)
        order.status = "COMPLETE"
        order.fill_price = event.fill_price
        order.fill_qty = event.fill_qty
        order.updated_at = now

        meta = _pending_order_meta.pop(event.kite_order_id, {})
        instrument_type = meta.get("instrument_type", "CE")
        entry_side = meta.get("entry_side", "BUY")

        instrument = (
            session.query(Instrument)
            .filter(
                Instrument.tradingsymbol == order.tradingsymbol,
                Instrument.exchange == order.exchange,
            )
            .first()
        )
        if instrument is None:
            log.error("_on_entry_filled: Instrument %s/%s not found", order.exchange, order.tradingsymbol)
            session.commit()
            return

        fill_price = event.fill_price
        rr = state.get_rr_ratio(settings.RR_RATIO)
        if instrument_type == "EQ":
            sl_distance = fill_price * settings.EQUITY_SL_PCT
            target_distance = rr * sl_distance
            sl_price = fill_price - sl_distance
            target_price = fill_price + target_distance
        elif instrument_type == "FUT":
            # sl_distance is always price-per-unit (FUTURES_SL_PCT × fill_price).
            # Do NOT pull from meta — the NG branch stored the lot-scaled INR value
            # which, subtracted from fill_price, produces a negative trigger price.
            sl_distance = fill_price * settings.FUTURES_SL_PCT
            target_distance = rr * sl_distance
            if entry_side == "BUY":
                sl_price = fill_price - sl_distance
                target_price = fill_price + target_distance
            else:  # short futures
                sl_price = fill_price + sl_distance
                target_price = fill_price - target_distance
        else:  # CE / PE options
            sl_risk = fill_price * state.get_sl_pct(settings.SL_PREMIUM_PCT)
            if entry_side == "BUY":
                # Long: SL when premium falls, target when it rises
                sl_price = fill_price - sl_risk
                target_price = fill_price + rr * sl_risk
            else:
                # Short (written option): SL when premium rises, target when it falls
                sl_price = fill_price + sl_risk
                target_price = fill_price - rr * sl_risk

        position = Position(
            order_id=order.id,
            exchange=order.exchange,
            tradingsymbol=order.tradingsymbol,
            underlying=meta.get("underlying", order.tradingsymbol),
            instrument_type=instrument_type,
            entry_premium=fill_price,
            current_sl=sl_price,
            quantity=event.fill_qty,
            lot_size=instrument.lot_size,
            opened_at=now,
            last_updated_at=now,
        )
        session.add(position)
        session.flush()  # get position.id

        kite_gtt_id = None
        gtt_error: str | None = None
        if not _dry_run(settings):
            from decimal import Decimal
            kite_client = get_session_manager().get_kite()
            try:
                kite_gtt_id = place_gtt_oco(
                    kite_client,
                    instrument,
                    event.fill_qty,
                    sl_trigger=Decimal(str(sl_price)),
                    sl_limit=Decimal(str(sl_price)),
                    target_trigger=Decimal(str(target_price)),
                    target_limit=Decimal(str(target_price)),
                    last_price=Decimal(str(fill_price)),
                    product=order.product,
                    entry_side=entry_side,
                )
            except Exception as exc:
                gtt_error = str(exc)
                log.error(
                    "_on_entry_filled: GTT placement failed for %s — %s. "
                    "Position saved without SL; manual intervention required.",
                    order.tradingsymbol, exc, exc_info=True,
                )
                session.add(AppError(
                    alert_id=order.alert_id,
                    error_type="GttPlacementError",
                    message=f"GTT OCO failed for {order.tradingsymbol}: {exc}",
                    traceback=traceback.format_exc(),
                    occurred_at=now,
                ))
        else:
            log.info(
                "_on_entry_filled DRY_RUN [%s]: would place GTT OCO sl=%.4f target=%.4f for %s",
                entry_side, sl_price, target_price, order.tradingsymbol,
            )

        gtt = Gtt(
            order_id=order.id,
            kite_gtt_id=kite_gtt_id,
            gtt_type="OCO",
            exchange=order.exchange,
            tradingsymbol=order.tradingsymbol,
            sl_trigger=sl_price,
            target_trigger=target_price,
            sl_order_price=sl_price,
            target_order_price=target_price,
            last_price_at_placement=fill_price,
            status="ACTIVE" if (not _dry_run(settings) and gtt_error is None) else ("GTT_FAILED" if gtt_error else "DRY_RUN"),
            placed_at=now,
            updated_at=now,
            dry_run=_dry_run(settings),
        )
        session.add(gtt)
        session.flush()  # get gtt.id

        position.gtt_id = gtt.id
        session.commit()


# ── GttFilledEvent callback ───────────────────────────────────────────────────

def _on_gtt_filled(event: GttFilledEvent) -> None:
    """Consume a GTT exit fill: compute PnL and persist via risk.record_trade_result."""
    if _SessionFactory is None:
        log.error("_on_gtt_filled fired but _SessionFactory is None")
        return
    with _SessionFactory() as session:
        position = (
            session.query(Position)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol == event.tradingsymbol,
                ClosedTrade.id == None,  # noqa: E711
            )
            .first()
        )
        if position is None:
            log.warning("_on_gtt_filled: no open position for %s", event.tradingsymbol)
            return

        entry_order = session.query(Order).filter(Order.id == position.order_id).first()
        if entry_order is None:
            log.error("_on_gtt_filled: no Order for position %d", position.id)
            return

        entry_price = Decimal(str(position.entry_premium))
        fill_price = Decimal(str(event.fill_price))
        qty = Decimal(str(event.fill_qty))
        # MCX: LTP and fill prices are per underlying unit; qty is in lots.
        # Multiply by units-per-lot to get true INR PnL.
        mcx_units = Decimal(str(
            get_settings().MCX_LOT_UNITS.get(position.underlying, 1)
            if position.exchange == "MCX" else 1
        ))

        if event.transaction_type == "SELL":
            pnl = (fill_price - entry_price) * qty * mcx_units
        else:
            pnl = -(fill_price - entry_price) * qty * mcx_units

        now = datetime.now(IST)
        risk.record_trade_result(session, entry_order.kite_order_id, pnl, now)
        session.commit()
        log.info(
            "_on_gtt_filled: %s pnl=%.2f order=%s",
            event.tradingsymbol, float(pnl), entry_order.kite_order_id,
        )


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SessionFactory, _watcher, _scheduler
    if _SessionFactory is None:
        from sqlalchemy.orm import sessionmaker
        engine = init_db(get_settings().DATABASE_URL)
        _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
    _watcher = OrderWatcher(on_entry_filled=_on_entry_filled, on_gtt_filled=_on_gtt_filled)
    state.set_trade_mode(get_settings().TRADE_MODE.value)
    _scheduler = make_scheduler(session_factory=_SessionFactory)
    _scheduler.start()
    daily_session_check(now=datetime.now(IST))
    # Start watcher immediately if a valid token is already stored, so fill
    # events are not missed after a container restart (the watcher is also
    # started in /kite/callback for fresh OAuth logins).
    if not state.SESSION_INVALID:
        try:
            result = get_session_manager()._load_token()
            if result is not None:
                _stored_token, _ = result
                _watcher.start(
                    api_key=get_settings().KITE_API_KEY,
                    access_token=_stored_token,
                )
                log.info("OrderWatcher started at startup with stored token")
        except Exception as _exc:
            log.warning("Could not start OrderWatcher at startup: %s", _exc)
    # Run instrument refresh in a background thread so the server starts
    # accepting webhooks immediately instead of blocking for 30+ seconds.
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, refresh_instruments_job, _SessionFactory)
    yield
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="tv-zerodha-bot", version="0.1.0", lifespan=lifespan)
_security = HTTPBasic(auto_error=False)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    body = await request.body()
    log.error(
        "422 validation error on %s | content-type=%r | body=%r | errors=%s",
        request.url.path,
        request.headers.get("content-type", "<missing>"),
        body[:500],
        exc.errors(),
    )
    safe_errors = [
        {**e, "input": e["input"].decode("utf-8", errors="replace") if isinstance(e.get("input"), bytes) else e.get("input")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})


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


def _build_alert_row(payload: AlertPayload, raw: str, ikey: str) -> Alert:
    return Alert(
        received_at=payload.timestamp,
        strategy_id=payload.alert_id,
        tv_ticker=payload.symbol,
        tv_exchange="",
        action=payload.action,
        order_type=None,
        entry_price=float(payload.price),
        stop_loss=None,
        sl_percent=None,
        atr=None,
        quantity_hint=None,
        product="NRML",
        tv_time=payload.timestamp.isoformat(),
        bar_time=None,
        interval=payload.timeframe,
        idempotency_key=ikey,
        raw_payload=raw,
        processed=False,
    )


# ── Background task ───────────────────────────────────────────────────────────

def _fail_alert(session: Any, alert: Alert, error_type: str, exc: BaseException) -> None:
    """Persist an AppError row, leave alert.processed=False, and log with traceback.

    Caller must commit() after this returns — keeps commit ownership at the call site.
    alert.processed is intentionally NOT set to True so the /dashboard shows the alert
    as unhandled and operators know it needs attention.
    """
    log.error("Alert %d [%s]: %s", alert.id, error_type, exc, exc_info=True)
    session.add(AppError(
        alert_id=alert.id,
        error_type=error_type,
        message=str(exc),
        traceback=traceback.format_exc(),
        occurred_at=datetime.now(IST),
    ))




def _process_alert(alert_id: int, alert_data: AlertPayload, settings: Settings) -> None:
    with _SessionFactory() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            log.error("Alert %d not found in DB (background task)", alert_id)
            return

        underlying = resolve_underlying(alert_data.symbol, settings=settings)
        now = alert_data.timestamp
        product = settings.PRODUCT_TYPE.value

        if state.is_emergency_stop():
            log.warning("Alert %d: EMERGENCY STOP active — all trading halted", alert_id)
            alert.processed = True
            session.commit()
            return

        if not _dry_run(settings):
            token_info = get_session_manager().get_token_info()
            if not token_info["is_valid"] or state.SESSION_INVALID:
                reason = token_info.get("reason") or "session marked invalid by scheduler"
                log.warning(
                    "Alert %d: order blocked — %s. Re-login at /kite/login",
                    alert_id, reason,
                )
                state.set_session_invalid(True)
                alert.processed = True
                session.commit()
                return

        if not _dry_run(settings):
            try:
                risk.check_risk_gates(session, now)
            except risk.RiskHaltError as exc:
                log.warning("Alert %d: risk halt — %s", alert_id, exc.reason)
                alert.processed = True
                session.commit()
                return

        # ── NATURALGAS / NATGASMINI: near-month future entry ─────────────────
        # NG options are illiquid; always trade the near-month FUT directly.
        # Other MCX commodities (CRUDEOIL, GOLD, SILVER, etc.) have liquid
        # options and fall through to the CE/PE path below.
        if underlying.is_natural_gas:
            try:
                resolved = resolve_expiry(
                    underlying.name, session, instrument_type="FUT",
                    segment=underlying.segment, now=now, settings=settings,
                )
            except NoEligibleExpiryError as exc:
                _fail_alert(session, alert, "NoEligibleExpiryError", exc)
                session.commit()
                return

            fut_instr = (
                session.query(Instrument)
                .filter(
                    Instrument.name == underlying.name,
                    Instrument.instrument_type == "FUT",
                    Instrument.exchange == underlying.segment,
                    Instrument.expiry == resolved.expiry_date,
                )
                .first()
            )
            if fut_instr is None:
                log.error("Alert %d: NG FUT instrument not found for expiry %s", alert_id, resolved.expiry_date)
                alert.processed = True
                session.commit()
                return

            sl_frac = settings.SL_PERCENT
            entry_px_d = alert_data.price
            # Kite's instruments.csv stores lot_size=1 for MCX futures; the true
            # contract size (MMBtu/lot) lives in MCX_LOT_UNITS.  SL must be in
            # INR-per-contract so compute_futures_qty produces contracts, not units.
            mcx_lot_units = Decimal(str(settings.MCX_LOT_UNITS.get(underlying.name, 1)))
            sl_distance_d = entry_px_d * sl_frac * mcx_lot_units
            sl_distance = float(sl_distance_d)
            target_distance = state.get_rr_ratio(settings.RR_RATIO) * sl_distance

            daily_remaining = risk.daily_loss_remaining(session, now)
            capital_risk = min(
                Decimal(str(settings.CAPITAL_PER_TRADE)) * settings.RISK_PCT,
                daily_remaining,
            )
            qty = risk.compute_futures_qty(capital_risk, sl_distance_d, Decimal(str(fut_instr.lot_size)))

            if qty < fut_instr.lot_size:
                log.warning("Alert %d: NG sizing — insufficient capital for 1 lot (qty=%d)", alert_id, qty)
                alert.processed = True
                session.commit()
                return

            qty = min(qty, state.get_max_lots(settings.MAX_LOTS_PER_TRADE) * fut_instr.lot_size)

            ng_side = "BUY" if alert_data.action == "BUY" else "SELL"
            kite_order_id = None
            if not _dry_run(settings):
                kite_client = get_session_manager().get_kite()
                kite_order_id = place_entry(kite_client, fut_instr, ng_side, qty, "MARKET", product)
                if kite_order_id:
                    _pending_order_meta[kite_order_id] = {
                        "instrument_type": "FUT",
                        "entry_side": ng_side,
                        "underlying": underlying.name,
                        "dry_run": False,
                    }
            else:
                log.info(
                    "Alert %d: DRY_RUN — would place MARKET %s %d lots %s (sl_dist=%.4f)",
                    alert_id, ng_side, qty // fut_instr.lot_size, fut_instr.tradingsymbol, sl_distance,
                )

            order = Order(
                alert_id=alert_id,
                kite_order_id=kite_order_id,
                variety="regular",
                exchange=fut_instr.exchange,
                tradingsymbol=fut_instr.tradingsymbol,
                transaction_type=ng_side,
                order_type="MARKET",
                product=product,
                quantity=qty,
                status="PENDING" if not _dry_run(settings) else "DRY_RUN",
                placed_at=now,
                updated_at=now,
                dry_run=_dry_run(settings),
            )
            session.add(order)
            session.flush()

            alert.processed = True
            session.commit()
            # Register AFTER commit so _on_entry_filled can find the Order row.
            # MARKET orders fill in milliseconds; committing first closes the race.
            if kite_order_id and not _dry_run(settings):
                if _watcher is not None:
                    _watcher.watch_order(kite_order_id, kite_fetcher=get_session_manager().get_kite)
                else:
                    log.error(
                        "Alert %d: _watcher is None — GTT will not be placed for %s",
                        alert_id, fut_instr.tradingsymbol,
                    )
            return

        # ── EQUITY (CNC) entry ────────────────────────────────────────────────
        # MCX symbols (CRUDEOILM, GOLD, etc.) sometimes arrive with instrument_type="EQUITY"
        # due to a TradingView alert template misconfiguration. Guard against misrouting them.
        if alert_data.instrument_type == "EQUITY" and underlying.segment != "MCX":
            if alert_data.action != "BUY":
                log.warning(
                    "Alert %d: EQUITY only supports BUY action (got %s) — skipping",
                    alert_id, alert_data.action,
                )
                alert.processed = True
                session.commit()
                return

            eq_instrument = (
                session.query(Instrument)
                .filter(
                    Instrument.tradingsymbol == underlying.name,
                    Instrument.exchange == "NSE",
                    Instrument.instrument_type == "EQ",
                )
                .first()
            )
            if eq_instrument is None:
                log.error(
                    "Alert %d: EQUITY — %s not found in NSE instruments table",
                    alert_id, underlying.name,
                )
                alert.processed = True
                session.commit()
                return

            if not _dry_run(settings):
                kite_client = get_session_manager().get_kite()
                q_key = f"NSE:{underlying.name}"
                q = kite_client.quote(q_key)
                ltp = float(q[q_key]["last_price"])
            else:
                ltp = float(alert_data.price)

            risk_amount = Decimal(str(settings.CAPITAL_PER_TRADE)) * settings.RISK_PCT
            sl_per_share = Decimal(str(ltp)) * Decimal(str(settings.EQUITY_SL_PCT))
            qty = floor(float(risk_amount / sl_per_share)) if sl_per_share > 0 else 0
            qty = min(qty, state.get_max_lots(settings.MAX_LOTS_PER_TRADE))
            if qty < 1:
                log.warning(
                    "Alert %d: EQUITY sizing — 0 shares at ltp=%.2f risk_amount=%.0f",
                    alert_id, ltp, float(risk_amount),
                )
                alert.processed = True
                session.commit()
                return

            kite_order_id = None
            if not _dry_run(settings):
                kite_order_id = place_entry(kite_client, eq_instrument, "BUY", qty, "MARKET", "CNC")
                if kite_order_id:
                    _pending_order_meta[kite_order_id] = {
                        "instrument_type": "EQ",
                        "underlying": underlying.name,
                        "dry_run": False,
                    }
            else:
                log.info(
                    "Alert %d: DRY_RUN — would BUY %d shares %s MARKET CNC at ltp=%.2f",
                    alert_id, qty, eq_instrument.tradingsymbol, ltp,
                )

            order = Order(
                alert_id=alert_id,
                kite_order_id=kite_order_id,
                variety="regular",
                exchange=eq_instrument.exchange,
                tradingsymbol=eq_instrument.tradingsymbol,
                transaction_type="BUY",
                order_type="MARKET",
                product="CNC",
                quantity=qty,
                status="PENDING" if not _dry_run(settings) else "DRY_RUN",
                placed_at=now,
                updated_at=now,
                dry_run=_dry_run(settings),
            )
            session.add(order)
            session.flush()

            alert.processed = True
            session.commit()
            # Register AFTER commit so _on_entry_filled can find the Order row.
            if kite_order_id and not _dry_run(settings):
                if _watcher is not None:
                    _watcher.watch_order(kite_order_id, kite_fetcher=get_session_manager().get_kite)
                else:
                    log.error(
                        "Alert %d: _watcher is None — GTT will not be placed for %s",
                        alert_id, eq_instrument.tradingsymbol,
                    )
            return

        # ── EXIT ──────────────────────────────────────────────────────────────
        if alert_data.action == "EXIT":
            ts = underlying.name
            position = (
                session.query(Position)
                .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
                .filter(Position.tradingsymbol == ts, ClosedTrade.id == None)  # noqa: E711
                .first()
            )
            if position is None:
                log.warning("Alert %d: EXIT — no open position found for %s", alert_id, ts)
                alert.processed = True
                session.commit()
                return

            gtt = (
                session.query(Gtt)
                .filter(Gtt.order_id == position.order_id, Gtt.status != "CANCELLED")
                .first()
            )
            instrument = (
                session.query(Instrument)
                .filter(Instrument.tradingsymbol == ts, Instrument.exchange == position.exchange)
                .first()
            )

            # Use the product stored on the original entry order so that CNC
            # equity exits use CNC (not the global NRML options product type).
            entry_order = session.query(Order).filter(Order.id == position.order_id).first()
            exit_product = entry_order.product if entry_order else product

            if not _dry_run(settings):
                kite_client = get_session_manager().get_kite()
                if gtt and gtt.kite_gtt_id:
                    cancel_gtt(kite_client, gtt.kite_gtt_id)
                if gtt:
                    gtt.status = "CANCELLED"
                if instrument:
                    square_off(kite_client, instrument, position.quantity, exit_product)
            else:
                log.info("Alert %d: DRY_RUN — would EXIT %s qty=%d product=%s", alert_id, ts, position.quantity, exit_product)

            ct = ClosedTrade(
                position_id=position.id,
                exchange=position.exchange,
                tradingsymbol=position.tradingsymbol,
                entry_premium=position.entry_premium,
                exit_premium=0.0,
                pnl=0.0,
                exit_reason="MANUAL_EXIT",
                opened_at=position.opened_at,
                closed_at=now,
            )
            session.add(ct)
            alert.processed = True
            session.commit()
            return

        # ── TRAIL ─────────────────────────────────────────────────────────────
        if alert_data.action == "TRAIL":
            ts = underlying.name
            position = (
                session.query(Position)
                .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
                .filter(Position.tradingsymbol == ts, ClosedTrade.id == None)  # noqa: E711
                .first()
            )
            if position is None:
                log.warning("Alert %d: TRAIL — no open position for %s", alert_id, ts)
                alert.processed = True
                session.commit()
                return

            gtt = (
                session.query(Gtt)
                .filter(Gtt.order_id == position.order_id, Gtt.status == "ACTIVE")
                .first()
            )
            instrument = (
                session.query(Instrument)
                .filter(Instrument.tradingsymbol == ts, Instrument.exchange == position.exchange)
                .first()
            )

            entry_premium = float(position.entry_premium)
            sl_risk = entry_premium * state.get_sl_pct(settings.SL_PREMIUM_PCT)
            current_premium = float(alert_data.premium) if alert_data.premium else entry_premium

            new_sl: float | None = None
            if current_premium >= entry_premium + settings.TRAIL_RR * sl_risk:
                candidate_sl = current_premium - settings.TRAIL_DISTANCE_RR * sl_risk
                if candidate_sl > float(position.current_sl):
                    new_sl = candidate_sl
                    position.trail_active = True
                    position.breakeven_moved = True
                    position.current_sl = new_sl
            elif (
                current_premium >= entry_premium + settings.BREAKEVEN_RR * sl_risk
                and not position.breakeven_moved
            ):
                new_sl = entry_premium
                position.breakeven_moved = True
                position.current_sl = new_sl

            if new_sl is not None and gtt is not None:
                target_price = float(gtt.target_order_price)
                if not _dry_run(settings) and instrument is not None:
                    kite_client = get_session_manager().get_kite()
                    modify_gtt(
                        kite_client,
                        gtt.kite_gtt_id,
                        sl_trigger=new_sl,
                        sl_limit=new_sl,
                        target_trigger=target_price,
                        target_limit=target_price,
                        last_price=current_premium,
                        instrument=instrument,
                        qty=position.quantity,
                        product=product,
                    )
                else:
                    log.info(
                        "Alert %d: DRY_RUN TRAIL — would set SL=%.4f target=%.4f for %s",
                        alert_id, new_sl, target_price, ts,
                    )
                gtt.sl_trigger = new_sl
                gtt.sl_order_price = new_sl
                gtt.modification_count += 1
                gtt.updated_at = now

            alert.processed = True
            session.commit()
            return

        # ── Non-NG options entry ───────────────────────────────────────────────
        trade_mode = state.get_trade_mode()
        if trade_mode == "BUY_OPTIONS":
            flag = "CE" if alert_data.action == "BUY" else "PE"
            entry_side = "BUY"
        else:  # SELL_OPTIONS: write the opposite type
            flag = "PE" if alert_data.action == "BUY" else "CE"
            entry_side = "SELL"
        log.info("[%s] Alert %d: %s signal → %s %s", trade_mode, alert_id, alert_data.action, entry_side, flag)
        target_delta = settings.TARGET_DELTA if trade_mode == "BUY_OPTIONS" else settings.SELL_OPTIONS_TARGET_DELTA
        delta_fallbacks = settings.DELTA_FALLBACK_STEPS if trade_mode == "BUY_OPTIONS" else settings.SELL_OPTIONS_DELTA_FALLBACK_STEPS

        if _dry_run(settings):
            log.info(
                "[%s] Alert %d: DRY_RUN — would %s %s option for %s (segment=%s)",
                trade_mode, alert_id, entry_side, flag, underlying.name, underlying.segment,
            )
            order = Order(
                alert_id=alert_id,
                kite_order_id=None,
                variety="regular",
                exchange=underlying.segment,
                tradingsymbol=underlying.name,
                transaction_type=entry_side,
                order_type="MARKET",
                product=product,
                quantity=0,
                status="DRY_RUN",
                placed_at=now,
                updated_at=now,
                dry_run=True,
            )
            session.add(order)
            alert.processed = True
            session.commit()
            return

        kite_client = get_session_manager().get_kite()

        # MCX commodities have no quotable spot index (unlike NSE:NIFTY 50).
        # Quote the near-month FUT tradingsymbol as a spot-price proxy instead.
        if underlying.segment == "MCX":
            mcx_spot_instr = (
                session.query(Instrument)
                .filter(
                    Instrument.name == underlying.name,
                    Instrument.instrument_type == "FUT",
                    Instrument.exchange == "MCX",
                    Instrument.expiry >= now.date(),
                )
                .order_by(Instrument.expiry)
                .first()
            )
            if mcx_spot_instr is None:
                log.error("Alert %d: no MCX FUT found for spot price of %s", alert_id, underlying.name)
                alert.processed = True
                session.commit()
                return
            spot_key = f"MCX:{mcx_spot_instr.tradingsymbol}"
            spot_quote = kite_client.quote(spot_key)
            spot_ltp = float(spot_quote[spot_key]["last_price"])
        else:
            spot_quote = kite_client.quote(underlying.spot_source)
            spot_ltp = float(spot_quote[underlying.spot_source]["last_price"])

        try:
            resolved = resolve_expiry(
                underlying.name, session, instrument_type=flag,
                segment=underlying.segment, now=now, settings=settings,
            )
        except NoEligibleExpiryError as exc:
            _fail_alert(session, alert, "NoEligibleExpiryError", exc)
            session.commit()
            return

        try:
            selection = select_strike(
                underlying.name,
                resolved.expiry_date,
                flag,
                kite_client,
                spot_ltp,
                session,
                alert_id=alert_id,
                segment=underlying.segment,
                target_delta=target_delta,
                settings=settings,
            )
        except NoValidStrikeError as exc:
            _fail_alert(session, alert, "NoValidStrikeError", exc)
            session.commit()
            return

        lot_size = selection.instrument.lot_size
        option_ltp = float(selection.option_ltp)
        # MCX: Kite quotes LTP per underlying unit (barrel/gram/kg) but orders are placed in lots.
        # Multiply by units-per-lot so sizing reflects the true premium per lot.
        mcx_units = settings.MCX_LOT_UNITS.get(underlying.name, 1) if underlying.segment == "MCX" else 1

        # Risk-based sizing: cap position so loss at SL ≤ capital_risk (mirrors NG futures).
        # For NSE (mcx_units=1): sl_per_unit = ltp×SL_PCT; compute_futures_qty divides by lot_size.
        # For MCX (lot_size=1):  sl_per_unit = ltp×mcx_units×SL_PCT (= risk per Kite lot directly).
        daily_remaining_opt = risk.daily_loss_remaining(session, now)
        capital_risk_opt = min(
            Decimal(str(settings.CAPITAL_PER_TRADE)) * settings.RISK_PCT,
            daily_remaining_opt,
        )
        sl_per_unit = Decimal(str(option_ltp * mcx_units)) * Decimal(str(state.get_sl_pct(settings.SL_PREMIUM_PCT)))
        qty = risk.compute_futures_qty(capital_risk_opt, sl_per_unit, Decimal(str(lot_size)))

        if qty < lot_size:
            log.warning(
                "Alert %d: options sizing — 0 lots at ltp=%.4f mcx_units=%d risk_budget=%.0f; "
                "retrying with lower deltas %s",
                alert_id, option_ltp, mcx_units,
                float(capital_risk_opt), delta_fallbacks,
            )
            fallback_selection = None
            for fallback_delta in delta_fallbacks:
                if fallback_delta >= target_delta:
                    continue
                try:
                    candidate = select_strike(
                        underlying.name,
                        resolved.expiry_date,
                        flag,
                        kite_client,
                        spot_ltp,
                        session,
                        alert_id=alert_id,
                        segment=underlying.segment,
                        target_delta=fallback_delta,
                        settings=settings,
                    )
                except NoValidStrikeError:
                    log.warning(
                        "Alert %d: no valid strike at fallback delta=%.2f",
                        alert_id, fallback_delta,
                    )
                    continue
                fallback_sl_per_unit = (
                    Decimal(str(float(candidate.option_ltp) * mcx_units))
                    * Decimal(str(state.get_sl_pct(settings.SL_PREMIUM_PCT)))
                )
                fallback_qty = risk.compute_futures_qty(
                    capital_risk_opt,
                    fallback_sl_per_unit,
                    Decimal(str(candidate.instrument.lot_size)),
                )
                if fallback_qty >= candidate.instrument.lot_size:
                    log.info(
                        "Alert %d: fallback delta=%.2f → %s ltp=%.4f fits risk_budget=%.0f",
                        alert_id, fallback_delta, candidate.instrument.tradingsymbol,
                        candidate.option_ltp, float(capital_risk_opt),
                    )
                    fallback_selection = candidate
                    break

            if fallback_selection is None:
                log.warning(
                    "Alert %d: no affordable strike at any delta %s within risk_budget=%.0f — skipping",
                    alert_id, delta_fallbacks, float(capital_risk_opt),
                )
                alert.processed = True
                session.commit()
                return

            selection = fallback_selection
            lot_size = selection.instrument.lot_size
            option_ltp = float(selection.option_ltp)
            sl_per_unit = (
                Decimal(str(option_ltp * mcx_units)) * Decimal(str(state.get_sl_pct(settings.SL_PREMIUM_PCT)))
            )
            qty = risk.compute_futures_qty(capital_risk_opt, sl_per_unit, Decimal(str(lot_size)))

        qty = min(qty, state.get_max_lots(settings.MAX_LOTS_PER_TRADE) * lot_size)

        lots_count = qty // lot_size
        total_premium = option_ltp * mcx_units * qty
        log.info(
            "[%s] Alert %d: placing %d lot(s) %s @ ltp=%.2f → total ₹%.0f (risk_budget=%.0f)",
            trade_mode, alert_id, lots_count, selection.instrument.tradingsymbol,
            option_ltp, total_premium, float(capital_risk_opt),
        )

        kite_order_id = place_entry(
            kite_client, selection.instrument, entry_side, qty, "MARKET", product
        )
        if kite_order_id:
            _pending_order_meta[kite_order_id] = {
                "instrument_type": flag,
                "underlying": underlying.name,
                "entry_side": entry_side,
                "dry_run": False,
            }

        order = Order(
            alert_id=alert_id,
            kite_order_id=kite_order_id,
            variety="regular",
            exchange=selection.instrument.exchange,
            tradingsymbol=selection.instrument.tradingsymbol,
            transaction_type=entry_side,
            order_type="MARKET",
            product=product,
            quantity=qty,
            status="PENDING",
            placed_at=now,
            updated_at=now,
            dry_run=False,
        )
        session.add(order)
        session.flush()

        alert.processed = True
        session.commit()
        # Register AFTER commit so _on_entry_filled can find the Order row.
        # MARKET orders fill in milliseconds; committing first closes the race.
        if kite_order_id:
            if _watcher is not None:
                _watcher.watch_order(kite_order_id, kite_fetcher=get_session_manager().get_kite)
            else:
                log.error(
                    "Alert %d: _watcher is None — GTT will not be placed for %s",
                    alert_id, selection.instrument.tradingsymbol,
                )


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

    # TradingView sends Content-Type: text/plain even for JSON bodies, so we
    # must parse manually instead of relying on FastAPI's automatic JSON binding.
    raw_body = await request.body()
    try:
        payload = AlertPayload.model_validate(json.loads(raw_body))
    except json.JSONDecodeError as exc:
        log.error(
            "webhook parse error | content-type=%r | body=%r | error=%s",
            request.headers.get("content-type", "<missing>"),
            raw_body[:500],
            exc,
        )
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}")
    except Exception as exc:
        log.error(
            "webhook validation error | content-type=%r | body=%r | error=%s",
            request.headers.get("content-type", "<missing>"),
            raw_body[:500],
            exc,
        )
        raise HTTPException(status_code=422, detail=str(exc))

    ikey = payload.alert_id
    if ikey in _idempotency_cache:
        return {"status": "duplicate"}
    _idempotency_cache[ikey] = True

    alert_row = _build_alert_row(payload, payload.model_dump_json(), ikey)
    session.add(alert_row)
    try:
        session.commit()
        session.refresh(alert_row)
    except IntegrityError:
        session.rollback()
        return {"status": "duplicate"}

    trades_today = _trades_today_count(session)
    if trades_today >= settings.MAX_TRADES_PER_DAY:
        log.warning(
            "Alert %d: Max daily trades reached (%d/%d) — rejecting",
            alert_row.id, trades_today, settings.MAX_TRADES_PER_DAY,
        )
        return {"status": "rejected", "reason": "max_daily_trades"}

    background_tasks.add_task(_process_alert, alert_row.id, payload, settings)

    return {"status": "queued", "alert_id": alert_row.id}


@app.get("/kite/login")
async def kite_login() -> Response:
    """Redirect the browser to Kite's OAuth login page.

    After the user authenticates, Kite redirects to KITE_REDIRECT_URL/kite/callback
    with ?status=success&request_token=<token>.
    """
    from kiteconnect import KiteConnect
    settings = get_settings()
    kite = KiteConnect(api_key=settings.KITE_API_KEY)
    login_url = kite.login_url()
    return Response(
        status_code=302,
        headers={"Location": login_url},
    )


@app.get("/kite/callback")
async def kite_callback(status: str = "unknown", request_token: str = "") -> Response:
    if status != "success" or not request_token:
        raise HTTPException(
            status_code=400,
            detail=f"Kite callback failed: status={status!r}",
        )
    try:
        access_token = get_session_manager().handle_callback(request_token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {exc}")
    settings = get_settings()
    if _watcher is not None:
        _watcher.restart(api_key=settings.KITE_API_KEY, access_token=access_token)
    # Validate the fresh token immediately so SESSION_INVALID is cleared and
    # checked_at reflects the actual login time, not the stale startup check.
    daily_session_check()
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


_CSS = (
    "*{box-sizing:border-box;margin:0;padding:0}"
    "body{font-family:'Inter',sans-serif;background:#f0f2f8;color:#2d3436;min-height:100vh}"
    ".hdr{background:linear-gradient(135deg,#1a1f3c 0%,#2d3561 100%);padding:14px 20px;"
    "display:flex;align-items:center;gap:10px}"
    ".hdr-icon{font-size:1.4em}"
    ".hdr-t{color:#fff;font-size:1.05em;font-weight:700;letter-spacing:.02em}"
    ".hdr-s{color:rgba(255,255,255,.5);font-size:.72em;margin-top:1px}"
    ".nav{background:#252b4a;display:flex;padding:0 12px;overflow-x:auto;"
    "scrollbar-width:none;-ms-overflow-style:none}"
    ".nav::-webkit-scrollbar{display:none}"
    ".nav a{color:rgba(255,255,255,.5);text-decoration:none;padding:10px 15px;font-size:.8em;"
    "font-weight:500;border-bottom:3px solid transparent;transition:all .2s;white-space:nowrap}"
    ".nav a:hover{color:#fff}"
    ".nav a.on{color:#a78bfa;border-bottom-color:#a78bfa}"
    ".wrap{padding:16px;margin:0 auto}"
    ".wrap-sm{max-width:560px}.wrap-lg{max-width:960px}"
    ".card{background:#fff;border-radius:12px;padding:16px 18px;margin-bottom:14px;"
    "box-shadow:0 2px 10px rgba(0,0,0,.06)}"
    ".ct{font-size:.68em;font-weight:700;text-transform:uppercase;letter-spacing:.09em;"
    "color:#8492a6;margin-bottom:12px;display:flex;align-items:center;gap:6px}"
    ".ct::before{content:'';display:block;width:3px;height:13px;border-radius:2px;background:#6c5ce7}"
    ".pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.73em;font-weight:700;letter-spacing:.02em}"
    ".pg{background:#e8faf5;color:#00875a}.pr{background:#fff0ed;color:#c0392b}"
    ".pa{background:#fff8e1;color:#b7791f}.pb{background:#eef2ff;color:#4c51bf}"
    ".pm{background:#f0f2f8;color:#636e72}.pp{background:#f3f0ff;color:#6c5ce7}"
    ".mr{display:flex;align-items:center;gap:10px;margin:8px 0}"
    ".ml{font-size:.78em;color:#8492a6;width:130px;flex-shrink:0}"
    ".mw{flex:1;height:5px;background:#e8ecf0;border-radius:3px;overflow:hidden}"
    ".mb{height:100%;border-radius:3px;transition:width .4s}"
    ".mv{font-size:.78em;font-weight:700;min-width:90px;text-align:right}"
    ".ok{color:#00b894}.wn{color:#f0a500}.bd{color:#e17055}"
    ".bok{background:#00b894}.bwn{background:#f0a500}.bbd{background:#e17055}"
    ".mdr{display:flex;align-items:center;justify-content:space-between;"
    "padding:9px 0;border-bottom:1px solid #f5f6fa}"
    ".mdr:last-of-type{border-bottom:none}"
    ".mdl{font-size:.82em;color:#444;font-weight:500;display:flex;align-items:center;gap:8px}"
    ".btn{padding:8px 16px;border:none;border-radius:8px;font-size:.8em;font-weight:700;"
    "cursor:pointer;transition:opacity .15s,transform .1s;color:#fff;"
    "font-family:'Inter',sans-serif;letter-spacing:.02em}"
    ".btn:active{transform:scale(.97)}.btn:hover{opacity:.88}"
    ".bn{background:linear-gradient(135deg,#2d3561,#1a1f3c)}"
    ".bg2{background:linear-gradient(135deg,#00cba1,#00b894)}"
    ".br2{background:linear-gradient(135deg,#ff6b6b,#e17055)}"
    ".ba{background:linear-gradient(135deg,#ffd93d,#f0a500);color:#333}"
    ".bp{background:linear-gradient(135deg,#a78bfa,#6c5ce7)}"
    ".bm{background:#b2bec3}"
    ".bfull{display:block;width:100%;padding:13px;font-size:.9em;margin:5px 0;text-align:center}"
    ".sbanner{background:linear-gradient(135deg,#e17055,#c0392b);color:#fff;"
    "padding:12px 16px;border-radius:10px;font-weight:700;text-align:center;"
    "margin-bottom:14px;font-size:.88em;letter-spacing:.02em;"
    "box-shadow:0 4px 14px rgba(225,112,85,.4)}"
    ".pr2{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #f5f6fa}"
    ".pr2:last-of-type{border-bottom:none}"
    ".pl{flex:1;font-size:.8em;color:#555}"
    ".pi{width:86px;padding:7px 8px;border:1.5px solid #e0e7ef;border-radius:7px;"
    "font-size:.84em;text-align:right;font-family:'Inter',sans-serif;transition:border-color .2s}"
    ".pi:focus{outline:none;border-color:#6c5ce7;box-shadow:0 0 0 3px rgba(108,92,231,.12)}"
    ".pu{font-size:.74em;color:#aaa;width:22px}"
    ".od{display:inline-block;width:6px;height:6px;border-radius:50%;"
    "background:#f0a500;margin-left:4px;vertical-align:middle}"
    "table{width:100%;border-collapse:collapse}"
    "th{font-size:.68em;font-weight:700;text-transform:uppercase;letter-spacing:.07em;"
    "color:#8492a6;padding:9px 10px;border-bottom:2px solid #f0f2f8;background:#fafbff;text-align:left}"
    "td{padding:9px 10px;font-size:.81em;border-bottom:1px solid #f5f6fa}"
    "tr:last-child td{border-bottom:none}"
    "tbody tr:hover td{background:#fafbff}"
    ".tc{text-align:center}.tr{text-align:right}"
)


def _shell(active: str, content: str, wide: bool = False, refresh: bool = False) -> str:
    """Wrap page content in the shared header / nav / CSS."""
    pages = [("control","Control"),("orders","Orders"),("gtts","GTTs"),
             ("history","History"),("dashboard","Alerts")]
    nav = "".join(
        "<a href='/" + p + "'" + (" class='on'" if p == active else "") + ">" + lbl + "</a>"
        for p, lbl in pages
    )
    wrap_cls = "wrap wrap-lg" if wide else "wrap wrap-sm"
    refresh_meta = "<meta http-equiv='refresh' content='30'>" if refresh else ""
    lut_html = (
        "<span id='lut' style='font-size:.68em;color:rgba(255,255,255,.35);"
        "margin-left:auto;padding:10px 14px;white-space:nowrap'></span>"
        if refresh else ""
    )
    lut_js = (
        "<script>window.addEventListener('load',function(){"
        "var e=document.getElementById('lut');"
        "if(e)e.textContent='Updated '+new Date().toLocaleTimeString();})"
        "</script>"
        if refresh else ""
    )
    return (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ZeroBot</title>"
        + refresh_meta +
        "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&amp;display=swap' rel='stylesheet'>"
        "<style>" + _CSS + "</style>"
        "</head><body>"
        "<div class='hdr'>"
        "<div class='hdr-icon'>&#x1F4C8;</div>"
        "<div><div class='hdr-t'>ZeroBot</div><div class='hdr-s'>Zerodha Algo Trading</div></div>"
        "</div>"
        "<div class='nav'>" + nav + lut_html + "</div>"
        "<div class='" + wrap_cls + "'>" + content + "</div>"
        + lut_js +
        "</body></html>"
    )


@app.get("/dashboard")
async def dashboard(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
) -> Response:
    rows = session.query(Alert).order_by(Alert.received_at.desc()).limit(50).all()
    rows_html = "".join(
        f"<tr><td>{r.id}</td><td style='font-weight:600'>{r.tv_ticker}</td><td>{r.action}</td>"
        f"<td>{r.received_at.strftime('%m/%d %H:%M') if r.received_at else '—'}</td>"
        f"<td><span class='pill {'pg' if r.processed else 'pa'}'>"
        f"{'YES' if r.processed else 'PENDING'}</span></td></tr>"
        for r in rows
    )
    return Response(
        content=_shell("dashboard",
            "<div class='card'><div class='ct'>Recent Alerts</div>"
            "<table><thead><tr><th>ID</th><th>Ticker</th><th>Action</th>"
            "<th>Time</th><th>Processed</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/positions")
async def positions(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
) -> Response:
    rows = session.query(Position).all()
    rows_html = "".join(
        f"<tr><td>{r.id}</td><td style='font-weight:600'>{r.tradingsymbol}</td>"
        f"<td class='tr'>{r.quantity}</td><td class='tr'>&#x20B9;{r.entry_premium:.2f}</td></tr>"
        for r in rows
    )
    return Response(
        content=_shell("",
            "<div class='card'><div class='ct'>Open Positions</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th>"
            "<th class='tr'>Qty</th><th class='tr'>Entry Premium</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/history")
async def history(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    days: int = Query(default=1, ge=0),
) -> Response:
    q = session.query(ClosedTrade)
    if days > 0:
        q = q.filter(ClosedTrade.closed_at >= datetime.now(IST) - timedelta(days=days))
    rows = q.order_by(ClosedTrade.closed_at.desc()).limit(200).all()

    total_pnl = sum((r.pnl or 0) for r in rows)
    pnl_vc = "ok" if total_pnl >= 0 else "bd"

    day_opts = [(1, "Today"), (3, "3 Days"), (7, "7 Days"), (0, "All")]
    filters = "<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px'>" + "".join(
        f"<a href='/history?days={d}' class='btn {'bp' if d == days else 'bm'}' "
        f"style='text-decoration:none'>{lbl}</a>"
        for d, lbl in day_opts
    ) + "</div>"

    summary = (
        f"<div class='card' style='display:flex;gap:16px;align-items:center;padding:11px 18px;margin-bottom:14px'>"
        f"<span style='font-size:.78em;color:#8492a6'>Period P&amp;L</span>"
        f"<span class='mv {pnl_vc}' style='font-size:.95em'>&#x20B9;{total_pnl:+.2f}</span>"
        f"<span style='font-size:.78em;color:#8492a6;margin-left:auto'>{len(rows)} trade(s)</span>"
        f"</div>"
    )

    rows_html = "".join(
        f"<tr><td>{r.id}</td><td style='font-weight:600'>{r.tradingsymbol}</td>"
        f"<td class='tr {'ok' if (r.pnl or 0) >= 0 else 'bd'}'>&#x20B9;{(r.pnl or 0):+.2f}</td>"
        f"<td>{r.exit_reason}</td>"
        f"<td>{r.closed_at.strftime('%m/%d %H:%M') if r.closed_at else '—'}</td></tr>"
        for r in rows
    )
    return Response(
        content=_shell("history",
            filters + summary
            + "<div class='card'><div class='ct'>Trade History</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th><th class='tr'>PnL</th>"
            "<th>Reason</th><th>Closed</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
        ),
        media_type="text/html",
    )


@app.get("/gtts")
async def gtts_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    show_all: int = Query(default=0, ge=0, le=1),
) -> Response:
    """Show GTT rows alongside live Kite status. Default: ACTIVE only."""
    q = session.query(Gtt).order_by(Gtt.placed_at.desc())
    if not show_all:
        q = q.filter(Gtt.status == "ACTIVE")
    rows = q.limit(100).all()

    live_map: dict[int, str] = {}
    if not _dry_run(settings):
        try:
            kite_client = get_session_manager().get_kite()
            for g in kite_client.get_gtts():
                live_map[g["id"]] = g["status"]
        except Exception as exc:
            log.warning("gtts_page: could not fetch live GTTs from Kite: %s", exc)

    def _gtt_pill(status: str) -> str:
        if status == "ACTIVE":
            return "pg"
        if status == "DRY_RUN":
            return "pa"
        return "pr"

    rows_html = "".join(
        "<tr>"
        f"<td>{r.id}</td>"
        f"<td style='font-weight:600'>{r.tradingsymbol}</td>"
        f"<td class='tr bd'>&#x20B9;{r.sl_trigger:.2f}</td>"
        f"<td class='tr ok'>&#x20B9;{r.target_trigger:.2f}</td>"
        f"<td class='tr'>&#x20B9;{r.last_price_at_placement:.2f}</td>"
        f"<td><span class='pill {_gtt_pill(r.status)}'>{r.status}</span></td>"
        f"<td class='tc'>{r.kite_gtt_id or '—'}</td>"
        f"<td class='tc'>{live_map.get(r.kite_gtt_id, 'N/A') if r.kite_gtt_id else '—'}</td>"
        f"<td>{r.placed_at.strftime('%m/%d %H:%M') if r.placed_at else '—'}</td>"
        "</tr>"
        for r in rows
    )
    toggle_lbl = "Show All" if not show_all else "Active Only"
    toggle_cls = "bm" if not show_all else "bp"
    toggle_bar = (
        f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:14px'>"
        f"<span style='font-size:.82em;color:#636e72'>{len(rows)} row(s) shown</span>"
        f"<a href='/gtts?show_all={1 - show_all}' class='btn {toggle_cls}' "
        f"style='text-decoration:none;margin-left:auto'>{toggle_lbl}</a></div>"
    )
    return Response(
        content=_shell("gtts",
            toggle_bar
            + "<div class='card'><div class='ct'>GTT / Stop-Loss Orders</div>"
            "<table><thead><tr><th>ID</th><th>Symbol</th><th class='tr'>SL Trigger</th>"
            "<th class='tr'>Target</th><th class='tr'>Entry Price</th><th>DB Status</th>"
            "<th class='tc'>Kite GTT ID</th><th class='tc'>Kite Live</th><th>Placed At</th>"
            "</tr></thead><tbody>" + rows_html + "</tbody></table>"
            "<p style='font-size:.73em;color:#aaa;margin-top:8px'>"
            "Kite Live: <b>active</b>=SL live | <b>triggered</b>=fired | "
            "<b>N/A</b>=not found (triggered+cleaned up or GTT_FAILED)</p></div>",
            wide=True,
            refresh=True,
        ),
        media_type="text/html",
    )


@app.get("/status")
async def status_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    """Legacy status page — redirects to /control."""
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/trade-mode/toggle")
async def toggle_trade_mode(
    _: None = Depends(_auth_guard),
) -> Response:
    new_mode = state.toggle_trade_mode()
    log.info("Trade mode toggled to %s", new_mode)
    return Response(status_code=302, headers={"Location": "/control"})


# ── /control — unified dashboard ──────────────────────────────────────────────



@app.get("/control")
async def control_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    from app.storage import AppError

    # ── live state ────────────────────────────────────────────────────────────
    trade_mode = state.get_trade_mode()
    paper = _dry_run(settings)
    estop = state.is_emergency_stop()
    overrides = state.get_all_overrides()

    eff_max_lots = state.get_max_lots(settings.MAX_LOTS_PER_TRADE)
    eff_max_loss = state.get_max_daily_loss(settings.MAX_DAILY_LOSS_ABS)
    eff_sl_pct   = state.get_sl_pct(settings.SL_PREMIUM_PCT)
    eff_rr       = state.get_rr_ratio(settings.RR_RATIO)

    # ── today's risk summary ──────────────────────────────────────────────────
    today_loss = _realised_loss_today(session)
    trades_today = _trades_today_count(session)
    consec = _consecutive_losses(session)
    open_pos = _open_position_count(session)
    loss_pct = (today_loss / eff_max_loss * 100) if eff_max_loss > 0 else 0

    # ── meter helpers ─────────────────────────────────────────────────────────
    def _bar(pct: float) -> tuple[str, str]:
        if pct >= 100:
            return "bbd", "bd"
        if pct >= 60:
            return "bwn", "wn"
        return "bok", "ok"

    loss_pct_c = min(loss_pct, 100)
    loss_bar, loss_vc = _bar(loss_pct)

    trades_pct = (trades_today / settings.MAX_TRADES_PER_DAY * 100) if settings.MAX_TRADES_PER_DAY else 0
    trades_bar, trades_vc = _bar(trades_pct)

    pos_pct = (open_pos / settings.MAX_OPEN_POSITIONS * 100) if settings.MAX_OPEN_POSITIONS else 0
    pos_bar, pos_vc = _bar(pos_pct)

    consec_pct = (consec / settings.CONSECUTIVE_LOSSES_LIMIT * 100) if settings.CONSECUTIVE_LOSSES_LIMIT else 0
    consec_bar, consec_vc = _bar(consec_pct)

    # ── badges / buttons ──────────────────────────────────────────────────────
    mode_pill    = "pg" if trade_mode == "BUY_OPTIONS" else "pr"
    mode_display = trade_mode.replace("_", " ")
    paper_pill   = "pa" if paper else "pp"
    paper_label  = "PAPER MODE" if paper else "LIVE TRADING"
    paper_lbl    = "Go LIVE" if paper else "Go PAPER"
    paper_cls    = "bg2" if paper else "ba"
    estop_lbl    = "&#x2714; Resume Trading" if estop else "&#x26D4; Emergency Stop"
    estop_cls    = "bg2" if estop else "br2"
    stop_banner  = (
        "<div class='sbanner'>&#x26D4; EMERGENCY STOP ACTIVE &mdash; No new trades will execute</div>"
        if estop else ""
    )

    def src(key: str) -> str:
        return (
            " <span style='color:#f0a500;font-size:.72em'>(overridden)</span>"
            if overrides.get(key) is not None else ""
        )

    # ── Kite session status ───────────────────────────────────────────────────
    try:
        token_info = get_session_manager().get_token_info()
        sess_valid = token_info["is_valid"]
        sess_age   = token_info.get("age_hours")
        sess_reason = token_info.get("reason") or ""
    except Exception:
        sess_valid = False
        sess_age   = None
        sess_reason = "unavailable"

    checked_at = get_last_checked_at()
    checked_str = (
        "Checked " + checked_at.astimezone(IST).strftime("%H:%M") if checked_at else "Not checked yet"
    )
    if sess_valid:
        sess_pill  = "pg"
        sess_label = f"Valid&ensp;({sess_age:.1f}h old)" if sess_age is not None else "Valid"
    else:
        sess_pill  = "pr"
        sess_label = f"Invalid &mdash; {sess_reason}" if sess_reason else "Invalid"

    # ── errors: last 48 h ─────────────────────────────────────────────────────
    err_cutoff = datetime.now(IST) - timedelta(hours=48)
    errors = (
        session.query(AppError)
        .filter(AppError.occurred_at >= err_cutoff)
        .order_by(AppError.occurred_at.desc())
        .limit(20)
        .all()
    )
    errors_html = "".join(
        f"<tr><td style='color:#8492a6;white-space:nowrap'>"
        f"{e.occurred_at.astimezone(IST).strftime('%m/%d %H:%M')}</td>"
        f"<td>{e.error_type}</td>"
        f"<td style='max-width:260px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis'>"
        f"{e.message[:140]}</td></tr>"
        for e in errors
    ) or "<tr><td colspan='3' style='text-align:center;color:#aaa'>No errors in last 48h</td></tr>"

    body = (
        stop_banner

        # risk summary
        + "<div class='card'><div class='ct'>Today's Risk Summary</div>"
        + f"<div class='mr'><div class='ml'>Daily loss</div>"
        + f"<div class='mw'><div class='mb {loss_bar}' style='width:{loss_pct_c:.0f}%'></div></div>"
        + f"<div class='mv {loss_vc}'>&#x20B9;{today_loss:.0f}&thinsp;/&thinsp;&#x20B9;{eff_max_loss:.0f}</div></div>"
        + f"<div class='mr'><div class='ml'>Trades today</div>"
        + f"<div class='mw'><div class='mb {trades_bar}' style='width:{min(trades_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {trades_vc}'>{trades_today}&thinsp;/&thinsp;{settings.MAX_TRADES_PER_DAY}</div></div>"
        + f"<div class='mr'><div class='ml'>Open positions</div>"
        + f"<div class='mw'><div class='mb {pos_bar}' style='width:{min(pos_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {pos_vc}'>{open_pos}&thinsp;/&thinsp;{settings.MAX_OPEN_POSITIONS}</div></div>"
        + f"<div class='mr'><div class='ml'>Consec. losses</div>"
        + f"<div class='mw'><div class='mb {consec_bar}' style='width:{min(consec_pct,100):.0f}%'></div></div>"
        + f"<div class='mv {consec_vc}'>{consec}&thinsp;/&thinsp;{settings.CONSECUTIVE_LOSSES_LIMIT}</div></div>"
        + "</div>"

        # kite session
        + "<div class='card'><div class='ct'>Kite Session</div>"
        + f"<div class='mdr'><div class='mdl'>Status&ensp;<span class='pill {sess_pill}'>{sess_label}</span>"
        + f"<span style='font-size:.72em;color:#aaa'>&ensp;{checked_str}</span></div>"
        + "<a href='/kite/login' class='btn bn' style='text-decoration:none'>Re-login</a></div>"
        + "</div>"

        # mode / switches
        + "<div class='card'><div class='ct'>Mode</div>"
        + f"<div class='mdr'><div class='mdl'>Trade Mode&ensp;<span class='pill {mode_pill}'>{mode_display}</span></div>"
        + "<form method='post' action='/trade-mode/toggle' style='margin:0'>"
        + "<button class='btn bn' type='submit'>Toggle</button></form></div>"
        + f"<div class='mdr'><div class='mdl'>Paper / Live&ensp;<span class='pill {paper_pill}'>{paper_label}</span></div>"
        + "<form method='post' action='/control/paper-mode/toggle' style='margin:0'>"
        + f"<button class='btn {paper_cls}' type='submit'>{paper_lbl}</button></form></div>"
        + "<div style='margin-top:10px'><form method='post' action='/control/emergency-stop/toggle'>"
        + f"<button class='btn bfull {estop_cls}' type='submit'>{estop_lbl}</button>"
        + "</form></div></div>"

        # risk params
        + "<div class='card'><div class='ct'>Risk Parameters</div>"
        + "<form method='post' action='/control/risk'>"
        + f"<div class='pr2'><div class='pl'>Max lots / trade{src('max_lots')}</div>"
        + f"<input class='pi' type='number' name='max_lots' value='{eff_max_lots}' min='1' max='20' step='1'>"
        + "<div class='pu'>lots</div></div>"
        + f"<div class='pr2'><div class='pl'>Max daily loss{src('max_daily_loss')}</div>"
        + f"<input class='pi' type='number' name='max_daily_loss' value='{eff_max_loss:.0f}' min='500' step='500'>"
        + "<div class='pu'>&#x20B9;</div></div>"
        + f"<div class='pr2'><div class='pl'>SL % (options){src('sl_pct')}</div>"
        + f"<input class='pi' type='number' name='sl_pct' value='{eff_sl_pct*100:.1f}' min='1' max='50' step='0.5'>"
        + "<div class='pu'>%</div></div>"
        + f"<div class='pr2'><div class='pl'>R:R ratio{src('rr_ratio')}</div>"
        + f"<input class='pi' type='number' name='rr_ratio' value='{eff_rr:.1f}' min='0.5' max='10' step='0.1'>"
        + "<div class='pu'>&#xD7;</div></div>"
        + "<div style='display:flex;gap:8px;margin-top:12px'>"
        + "<button class='btn bp' type='submit' style='flex:1'>Apply</button>"
        + "<button class='btn bm' type='submit' name='reset' value='1' style='flex:1'>Reset to defaults</button>"
        + "</div></form></div>"

        # errors
        + "<div class='card'><div class='ct'>Recent Errors</div>"
        + "<table><thead><tr><th>Time</th><th>Type</th><th>Message</th></tr></thead><tbody>"
        + errors_html + "</tbody></table></div>"
    )
    return Response(content=_shell("control", body, refresh=True), media_type="text/html")


@app.post("/control/paper-mode/toggle")
async def toggle_paper_mode(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    current = _dry_run(settings)
    state.set_paper_mode(not current)
    log.info("Paper mode toggled to %s", not current)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/emergency-stop/toggle")
async def toggle_emergency_stop(_: None = Depends(_auth_guard)) -> Response:
    new_val = not state.is_emergency_stop()
    state.set_emergency_stop(new_val)
    log.warning("Emergency stop set to %s", new_val)
    return Response(status_code=302, headers={"Location": "/control"})


@app.post("/control/risk")
async def update_risk(
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
    reset: str = Form(default=""),
    max_lots: str = Form(default=""),
    max_daily_loss: str = Form(default=""),
    sl_pct: str = Form(default=""),
    rr_ratio: str = Form(default=""),
) -> Response:
    if reset == "1":
        state.set_max_lots(None)
        state.set_max_daily_loss(None)
        state.set_sl_pct(None)
        state.set_rr_ratio(None)
        log.info("Risk params reset to .env defaults")
    else:
        try:
            if max_lots.strip():
                state.set_max_lots(int(max_lots))
            if max_daily_loss.strip():
                state.set_max_daily_loss(float(max_daily_loss))
            if sl_pct.strip():
                state.set_sl_pct(float(sl_pct) / 100.0)
            if rr_ratio.strip():
                state.set_rr_ratio(float(rr_ratio))
            log.info("Risk params updated: max_lots=%s max_loss=%s sl_pct=%s rr=%s",
                     max_lots, max_daily_loss, sl_pct, rr_ratio)
        except ValueError:
            pass
    return Response(status_code=302, headers={"Location": "/control"})


# ── /orders — consolidated trade view ────────────────────────────────────────

@app.get("/orders")
async def orders_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    days: int = Query(default=1, ge=0),
) -> Response:
    q = (
        session.query(Order, Position, Gtt, ClosedTrade)
        .outerjoin(Position, Position.order_id == Order.id)
        .outerjoin(Gtt, Gtt.order_id == Order.id)
        .outerjoin(ClosedTrade, ClosedTrade.position_id == Position.id)
    )
    if days > 0:
        q = q.filter(Order.placed_at >= datetime.now(IST) - timedelta(days=days))
    rows = q.order_by(Order.placed_at.desc()).limit(200).all()

    def pnl_cls(pnl):
        if pnl is None:
            return ""
        return "ok" if pnl >= 0 else "bd"

    def status_badge(order, ct):
        if ct is not None:
            reason = ct.exit_reason or "CLOSED"
            pill_cls = "pg" if (ct.pnl or 0) >= 0 else "pr"
            return f"<span class='pill {pill_cls}'>{reason}</span>"
        s = order.status
        if s == "DRY_RUN":
            return "<span class='pill pa'>PAPER</span>"
        if s in ("PENDING", "COMPLETE"):
            return "<span class='pill pb'>OPEN</span>"
        return f"<span class='pill pm'>{s}</span>"

    # summary stats
    total_pnl  = sum((ct.pnl or 0) for _, _, _, ct in rows if ct is not None)
    open_count = sum(1 for _, pos, _, ct in rows if pos is not None and ct is None)
    closed_count = sum(1 for _, _, _, ct in rows if ct is not None)
    pnl_vc = "ok" if total_pnl >= 0 else "bd"

    day_opts = [(1, "Today"), (3, "3 Days"), (7, "7 Days"), (0, "All")]
    filters = "<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px'>" + "".join(
        f"<a href='/orders?days={d}' class='btn {'bp' if d == days else 'bm'}' "
        f"style='text-decoration:none'>{lbl}</a>"
        for d, lbl in day_opts
    ) + "</div>"

    summary = (
        f"<div class='card' style='display:flex;gap:16px;align-items:center;padding:11px 18px;margin-bottom:14px'>"
        f"<span style='font-size:.78em;color:#8492a6'>Period P&amp;L</span>"
        f"<span class='mv {pnl_vc}' style='font-size:.95em'>&#x20B9;{total_pnl:+.2f}</span>"
        f"<span style='font-size:.78em;color:#8492a6;margin-left:auto'>"
        f"Open: <b>{open_count}</b>&ensp;Closed: <b>{closed_count}</b></span>"
        f"</div>"
    )

    rows_html = ""
    for order, pos, gtt, ct in rows:
        entry_px = f"₹{pos.entry_premium:.2f}" if pos else "—"
        sl       = f"₹{gtt.sl_trigger:.2f}" if gtt else "—"
        tgt      = f"₹{gtt.target_trigger:.2f}" if gtt else "—"
        pnl_val  = ct.pnl if ct else None
        pnl_str  = f"₹{pnl_val:+.2f}" if pnl_val is not None else "—"
        lots     = (pos.quantity // pos.lot_size) if (pos and pos.lot_size) else (order.quantity or "—")
        t        = order.placed_at.strftime("%m/%d %H:%M") if order.placed_at else "—"
        rows_html += (
            f"<tr>"
            f"<td>{t}</td>"
            f"<td style='font-weight:600'>{order.tradingsymbol}</td>"
            f"<td>{order.transaction_type}</td>"
            f"<td class='tr'>{lots}</td>"
            f"<td class='tr'>{entry_px}</td>"
            f"<td class='tr bd'>{sl}</td>"
            f"<td class='tr ok'>{tgt}</td>"
            f"<td class='tr {pnl_cls(pnl_val)}'>{pnl_str}</td>"
            f"<td>{status_badge(order, ct)}</td>"
            f"</tr>"
        )

    return Response(
        content=_shell("orders",
            filters + summary
            + "<div class='card'><div class='ct'>Orders</div>"
            "<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th class='tr'>Lots</th>"
            "<th class='tr'>Entry</th><th class='tr'>SL</th><th class='tr'>Target</th>"
            "<th class='tr'>P&amp;L</th><th>Status</th></tr></thead>"
            "<tbody>" + rows_html + "</tbody></table></div>",
            wide=True,
            refresh=True,
        ),
        media_type="text/html",
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/auth/status")
async def auth_status(settings: Settings = Depends(get_current_settings)) -> dict:
    try:
        token_info = get_session_manager().get_token_info()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Session manager not configured: {exc}")
    checked_at = get_last_checked_at()
    return {
        "session_valid": token_info["is_valid"],
        "token_age_hours": token_info["age_hours"],
        "reason": token_info["reason"],
        "dry_run": settings.DRY_RUN,
        "checked_at": checked_at.isoformat() if checked_at is not None else None,
    }
