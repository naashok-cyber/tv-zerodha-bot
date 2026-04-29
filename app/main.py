from __future__ import annotations

import asyncio
import json
import logging
import secrets
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from math import floor
from typing import Any

from cachetools import TTLCache
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
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
    loss = _realised_loss_today(session)
    if loss >= settings.effective_max_daily_loss:
        return False, f"daily loss ₹{loss:.2f} >= limit ₹{settings.effective_max_daily_loss:.2f}"
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
        if instrument_type == "EQ":
            sl_distance = fill_price * settings.EQUITY_SL_PCT
            target_distance = settings.RR_RATIO * sl_distance
            sl_price = fill_price - sl_distance
            target_price = fill_price + target_distance
        elif instrument_type == "FUT":
            sl_distance = meta.get("sl_distance", fill_price * settings.FUTURES_SL_PCT)
            target_distance = meta.get("target_distance", settings.RR_RATIO * sl_distance)
            sl_price = fill_price - sl_distance
            target_price = fill_price + target_distance
        else:  # CE / PE options
            sl_risk = fill_price * settings.SL_PREMIUM_PCT
            sl_price = fill_price - sl_risk
            target_price = fill_price + settings.RR_RATIO * sl_risk

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
        if not settings.DRY_RUN:
            from decimal import Decimal
            kite_client = get_session_manager().get_kite()
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
            )
        else:
            log.info(
                "_on_entry_filled DRY_RUN: would place GTT OCO sl=%.4f target=%.4f for %s",
                sl_price, target_price, order.tradingsymbol,
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
            status="ACTIVE" if not settings.DRY_RUN else "DRY_RUN",
            placed_at=now,
            updated_at=now,
            dry_run=settings.DRY_RUN,
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

        if event.transaction_type == "SELL":
            pnl = (fill_price - entry_price) * qty
        else:
            pnl = -(fill_price - entry_price) * qty

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

        if state.SESSION_INVALID and not settings.DRY_RUN:
            log.warning("Alert %d: order blocked — session invalid, manual login required", alert_id)
            alert.processed = True
            session.commit()
            return

        if not settings.DRY_RUN:
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
            sl_distance_d = entry_px_d * sl_frac
            sl_distance = float(sl_distance_d)
            target_distance = settings.RR_RATIO * sl_distance

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

            kite_order_id = None
            if not settings.DRY_RUN:
                kite_client = get_session_manager().get_kite()
                kite_order_id = place_entry(kite_client, fut_instr, "BUY", qty, "MARKET", product)
            else:
                log.info(
                    "Alert %d: DRY_RUN — would place MARKET BUY %d lots %s (sl_dist=%.4f)",
                    alert_id, qty // fut_instr.lot_size, fut_instr.tradingsymbol, sl_distance,
                )

            order = Order(
                alert_id=alert_id,
                kite_order_id=kite_order_id,
                variety="regular",
                exchange=fut_instr.exchange,
                tradingsymbol=fut_instr.tradingsymbol,
                transaction_type="BUY",
                order_type="MARKET",
                product=product,
                quantity=qty,
                status="PENDING" if not settings.DRY_RUN else "DRY_RUN",
                placed_at=now,
                updated_at=now,
                dry_run=settings.DRY_RUN,
            )
            session.add(order)
            session.flush()

            if kite_order_id and _watcher is not None:
                _pending_order_meta[kite_order_id] = {
                    "instrument_type": "FUT",
                    "sl_distance": sl_distance,
                    "target_distance": target_distance,
                    "order_db_id": order.id,
                    "underlying": underlying.name,
                    "dry_run": settings.DRY_RUN,
                }
                _watcher.watch_order(kite_order_id)

            alert.processed = True
            session.commit()
            return

        # ── EQUITY (CNC) entry ────────────────────────────────────────────────
        if alert_data.instrument_type == "EQUITY":
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

            if not settings.DRY_RUN:
                kite_client = get_session_manager().get_kite()
                q_key = f"NSE:{underlying.name}"
                q = kite_client.quote(q_key)
                ltp = float(q[q_key]["last_price"])
            else:
                ltp = float(alert_data.price)

            qty = risk.compute_equity_qty(
                Decimal(str(settings.CAPITAL_PER_TRADE)),
                Decimal(str(ltp)),
            )
            if qty < 1:
                log.warning(
                    "Alert %d: EQUITY sizing — 0 shares at ltp=%.2f CAPITAL=%.0f",
                    alert_id, ltp, settings.CAPITAL_PER_TRADE,
                )
                alert.processed = True
                session.commit()
                return

            kite_order_id = None
            if not settings.DRY_RUN:
                kite_order_id = place_entry(kite_client, eq_instrument, "BUY", qty, "MARKET", "CNC")
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
                status="PENDING" if not settings.DRY_RUN else "DRY_RUN",
                placed_at=now,
                updated_at=now,
                dry_run=settings.DRY_RUN,
            )
            session.add(order)
            session.flush()

            if kite_order_id and _watcher is not None:
                _pending_order_meta[kite_order_id] = {
                    "instrument_type": "EQ",
                    "order_db_id": order.id,
                    "underlying": underlying.name,
                    "dry_run": settings.DRY_RUN,
                }
                _watcher.watch_order(kite_order_id)

            alert.processed = True
            session.commit()
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

            if not settings.DRY_RUN:
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
            sl_risk = entry_premium * settings.SL_PREMIUM_PCT
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
                if not settings.DRY_RUN and instrument is not None:
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

        # ── Non-NG options entry (BUY → CE, SELL → PE) ───────────────────────
        flag = "CE" if alert_data.action == "BUY" else "PE"

        if settings.DRY_RUN:
            log.info(
                "Alert %d: DRY_RUN — would place %s %s option for %s (segment=%s)",
                alert_id, alert_data.action, flag, underlying.name, underlying.segment,
            )
            order = Order(
                alert_id=alert_id,
                kite_order_id=None,
                variety="regular",
                exchange=underlying.segment,
                tradingsymbol=underlying.name,
                transaction_type="BUY",
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
                target_delta=settings.TARGET_DELTA,
                settings=settings,
            )
        except NoValidStrikeError as exc:
            _fail_alert(session, alert, "NoValidStrikeError", exc)
            session.commit()
            return

        lot_size = selection.instrument.lot_size
        option_ltp = float(selection.option_ltp)
        qty = risk.compute_option_qty(
            Decimal(str(settings.CAPITAL_PER_TRADE)),
            Decimal(str(option_ltp)),
            Decimal(str(lot_size)),
        )

        if qty < lot_size:
            log.warning(
                "Alert %d: options sizing — 0 lots at ltp=%.4f CAPITAL=%.0f",
                alert_id, option_ltp, settings.CAPITAL_PER_TRADE,
            )
            alert.processed = True
            session.commit()
            return

        kite_order_id = place_entry(
            kite_client, selection.instrument, "BUY", qty, "MARKET", product
        )

        order = Order(
            alert_id=alert_id,
            kite_order_id=kite_order_id,
            variety="regular",
            exchange=selection.instrument.exchange,
            tradingsymbol=selection.instrument.tradingsymbol,
            transaction_type="BUY",
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

        if kite_order_id and _watcher is not None:
            _pending_order_meta[kite_order_id] = {
                "instrument_type": flag,
                "order_db_id": order.id,
                "underlying": underlying.name,
                "dry_run": False,
            }
            _watcher.watch_order(kite_order_id)

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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/auth/status")
async def auth_status(settings: Settings = Depends(get_current_settings)) -> dict:
    checked_at = get_last_checked_at()
    return {
        "session_valid": not state.SESSION_INVALID,
        "dry_run": settings.DRY_RUN,
        "checked_at": checked_at.isoformat() if checked_at is not None else None,
    }
