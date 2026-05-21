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
from datetime import datetime, timezone
from decimal import Decimal
from math import floor
from typing import Any

from cachetools import TTLCache
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, Response
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


_NAV = (
    "<nav style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px'>"
    "<a href='/control' style='padding:6px 12px;background:#333;color:#fff;border-radius:6px;text-decoration:none;font-size:.9em'>Control</a>"
    "<a href='/orders' style='padding:6px 12px;background:#fff;border:1px solid #ccc;border-radius:6px;text-decoration:none;color:#333;font-size:.9em'>Orders</a>"
    "<a href='/gtts' style='padding:6px 12px;background:#fff;border:1px solid #ccc;border-radius:6px;text-decoration:none;color:#333;font-size:.9em'>GTTs</a>"
    "<a href='/history' style='padding:6px 12px;background:#fff;border:1px solid #ccc;border-radius:6px;text-decoration:none;color:#333;font-size:.9em'>History</a>"
    "<a href='/dashboard' style='padding:6px 12px;background:#fff;border:1px solid #ccc;border-radius:6px;text-decoration:none;color:#333;font-size:.9em'>Alerts</a>"
    "</nav>"
)
_PAGE_HEAD = (
    "<html><head>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<style>"
    "body{font-family:sans-serif;padding:16px;max-width:900px;margin:auto;background:#f5f5f5}"
    "h2{margin:0 0 4px}h3{margin:14px 0 6px;color:#555;font-size:.95em;text-transform:uppercase;letter-spacing:.04em}"
    "table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}"
    "th{background:#f0f0f0;padding:8px 10px;text-align:left;font-size:.85em}"
    "td{padding:7px 10px;border-top:1px solid #f0f0f0;font-size:.85em}"
    "tr:hover td{background:#fafafa}"
    "</style></head><body>"
)


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
            _PAGE_HEAD + _NAV
            + "<h2>Recent Alerts</h2>"
            "<table><tr><th>ID</th><th>Ticker</th><th>Action</th>"
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
            _PAGE_HEAD + _NAV
            + "<h2>Open Positions</h2>"
            "<table><tr><th>ID</th><th>Symbol</th>"
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
            _PAGE_HEAD + _NAV
            + "<h2>Trade History</h2>"
            "<table><tr><th>ID</th><th>Symbol</th><th>PnL</th>"
            f"<th>Reason</th><th>Closed</th></tr>{rows_html}</table></body></html>"
        ),
        media_type="text/html",
    )


@app.get("/gtts")
async def gtts_page(
    session: Session = Depends(get_db_session),
    _: None = Depends(_auth_guard),
    settings: Settings = Depends(get_current_settings),
) -> Response:
    """Show every GTT row from the DB alongside its live status on Kite."""
    rows = session.query(Gtt).order_by(Gtt.placed_at.desc()).limit(50).all()

    live_map: dict[int, str] = {}
    if not _dry_run(settings):
        try:
            kite_client = get_session_manager().get_kite()
            for g in kite_client.get_gtts():
                live_map[g["id"]] = g["status"]
        except Exception as exc:
            log.warning("gtts_page: could not fetch live GTTs from Kite: %s", exc)

    rows_html = "".join(
        "<tr>"
        f"<td>{r.id}</td>"
        f"<td>{r.tradingsymbol}</td>"
        f"<td>{r.sl_trigger:.2f}</td>"
        f"<td>{r.target_trigger:.2f}</td>"
        f"<td>{r.last_price_at_placement:.2f}</td>"
        f"<td>{r.status}</td>"
        f"<td>{r.kite_gtt_id or '-'}</td>"
        f"<td>{live_map.get(r.kite_gtt_id, 'N/A') if r.kite_gtt_id else '-'}</td>"
        f"<td>{r.placed_at}</td>"
        "</tr>"
        for r in rows
    )
    return Response(
        content=(
            _PAGE_HEAD + _NAV
            + "<h2>GTT / Stop-Loss Orders</h2>"
            "<table>"
            "<tr><th>ID</th><th>Symbol</th><th>SL Trigger</th><th>Target Trigger</th>"
            "<th>Entry Price</th><th>DB Status</th><th>Kite GTT ID</th>"
            f"<th>Kite Live Status</th><th>Placed At</th></tr>{rows_html}"
            "</table>"
            "<p style='font-size:.8em;color:#888'>Kite Live: <b>active</b>=SL live | "
            "<b>triggered</b>=fired | "
            "<b>N/A</b>=not found (triggered+cleaned up, or GTT_FAILED)</p>"
            "</body></html>"
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

_CTRL_CSS = (
    "<style>"
    "body{font-family:sans-serif;padding:16px;max-width:520px;margin:auto;background:#f5f5f5}"
    "h2{margin:0 0 4px}.card{background:#fff;border-radius:10px;padding:16px;margin-bottom:14px;"
    "box-shadow:0 1px 3px rgba(0,0,0,.1)}.card h3{margin:0 0 10px;font-size:.85em;color:#777;"
    "text-transform:uppercase;letter-spacing:.05em}"
    ".btn{display:block;width:100%;padding:14px;font-size:1em;border:none;border-radius:8px;"
    "cursor:pointer;margin:6px 0;color:#fff;font-weight:bold}"
    ".btn-green{background:#28a745}.btn-red{background:#dc3545}.btn-orange{background:#e67e22}"
    ".btn-gray{background:#6c757d}.btn-blue{background:#007bff}"
    ".badge{display:inline-block;padding:5px 14px;border-radius:20px;font-weight:bold;font-size:.95em}"
    ".bg{background:#d4edda;color:#155724}.br{background:#f8d7da;color:#721c24}"
    ".bo{background:#fff3cd;color:#856404}"
    ".switch-row{display:flex;align-items:center;justify-content:space-between;margin:8px 0}"
    ".switch-row label{font-size:.9em;color:#444}"
    ".sw{position:relative;display:inline-block;width:52px;height:28px}"
    ".sw input{opacity:0;width:0;height:0}"
    ".slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#ccc;"
    "border-radius:28px;transition:.3s}"
    ".slider:before{position:absolute;content:'';height:22px;width:22px;left:3px;bottom:3px;"
    "background:white;border-radius:50%;transition:.3s}"
    "input:checked+.slider{background:#28a745}"
    "input:checked+.slider:before{transform:translateX(24px)}"
    ".risk-row{display:flex;align-items:center;gap:8px;margin:8px 0}"
    ".risk-row label{flex:1;font-size:.88em;color:#555}"
    ".risk-row input[type=number]{width:90px;padding:6px 8px;border:1px solid #ccc;"
    "border-radius:6px;font-size:.92em;text-align:right}"
    ".risk-row .unit{color:#999;font-size:.82em;min-width:28px}"
    ".apply-btn{padding:8px 16px;font-size:.9em;border:none;border-radius:6px;cursor:pointer;"
    "background:#007bff;color:#fff;margin-top:6px}"
    ".reset-btn{padding:8px 16px;font-size:.9em;border:none;border-radius:6px;cursor:pointer;"
    "background:#6c757d;color:#fff;margin-top:6px;margin-left:6px}"
    ".sum-row{display:flex;justify-content:space-between;padding:5px 0;"
    "border-bottom:1px solid #f0f0f0;font-size:.88em}"
    ".sum-row:last-child{border-bottom:none}"
    ".ok{color:#28a745;font-weight:bold}.warn{color:#e67e22;font-weight:bold}"
    ".bad{color:#dc3545;font-weight:bold}"
    ".stop-banner{background:#f8d7da;color:#721c24;padding:10px 14px;border-radius:8px;"
    "font-weight:bold;margin-bottom:12px;text-align:center;font-size:1.05em}"
    "nav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}"
    "nav a{padding:6px 12px;background:#fff;border:1px solid #ccc;border-radius:6px;"
    "text-decoration:none;color:#333;font-size:.88em}"
    "nav a.active{background:#333;color:#fff;border-color:#333}"
    "</style>"
)

_CTRL_NAV = (
    "<nav>"
    "<a href='/control' class='active'>Control</a>"
    "<a href='/orders'>Orders</a>"
    "<a href='/gtts'>GTTs</a>"
    "<a href='/history'>History</a>"
    "<a href='/dashboard'>Alerts</a>"
    "</nav>"
)


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

    def risk_cls(used, cap):
        r = used / cap if cap else 0
        return "bad" if r >= 1 else "warn" if r >= 0.6 else "ok"

    def consec_cls(c):
        return "bad" if c >= settings.CONSECUTIVE_LOSSES_LIMIT else "warn" if c >= 2 else "ok"

    errors = session.query(AppError).order_by(AppError.occurred_at.desc()).limit(5).all()
    errors_html = "".join(
        f"<tr><td style='font-size:.8em;color:#888'>{e.occurred_at.strftime('%H:%M')}</td>"
        f"<td style='font-size:.8em'>{e.error_type}</td>"
        f"<td style='font-size:.8em;max-width:200px;overflow:hidden;white-space:nowrap;"
        f"text-overflow:ellipsis'>{e.message[:80]}</td></tr>"
        for e in errors
    ) or "<tr><td colspan='3' style='color:#888;font-size:.85em'>No errors</td></tr>"

    # ── trade-mode badge ──────────────────────────────────────────────────────
    if trade_mode == "BUY_OPTIONS":
        mode_badge = "<span class='badge bg'>BUY OPTIONS</span>"
        mode_btn   = "<button class='btn btn-red' type='submit'>Switch to SELL OPTIONS</button>"
    else:
        mode_badge = "<span class='badge br'>SELL OPTIONS</span>"
        mode_btn   = "<button class='btn btn-green' type='submit'>Switch to BUY OPTIONS</button>"

    # ── paper/live badge ──────────────────────────────────────────────────────
    paper_label  = "PAPER MODE" if paper else "LIVE TRADING"
    paper_badge  = f"<span class='badge {'bo' if paper else 'bg'}'>{paper_label}</span>"
    paper_btn_lbl = "Switch to LIVE TRADING" if paper else "Switch to PAPER MODE"
    paper_btn_cls = "btn-green" if paper else "btn-orange"

    # ── emergency stop ────────────────────────────────────────────────────────
    stop_banner = (
        "<div class='stop-banner'>⛔ EMERGENCY STOP ACTIVE — No new trades will execute</div>"
        if estop else ""
    )
    estop_btn_lbl = "Resume Trading" if estop else "Emergency Stop"
    estop_btn_cls = "btn-green" if estop else "btn-red"

    # ── override indicators ───────────────────────────────────────────────────
    def src(key):
        return " <small style='color:#e67e22'>(overridden)</small>" if overrides.get(key) is not None else ""

    html = (
        f"<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Bot Control</title>{_CTRL_CSS}</head><body>"
        f"{_CTRL_NAV}"
        f"{stop_banner}"
        f"<h2>Bot Control</h2>"

        # ── Risk summary card ─────────────────────────────────────────────────
        f"<div class='card'>"
        f"<h3>Today's Risk Summary</h3>"
        f"<div class='sum-row'><span>Daily loss</span>"
        f"<span class='{risk_cls(today_loss, eff_max_loss)}'>₹{today_loss:.0f} / ₹{eff_max_loss:.0f}"
        f" ({loss_pct:.0f}%)</span></div>"
        f"<div class='sum-row'><span>Trades today</span>"
        f"<span class='{risk_cls(trades_today, settings.MAX_TRADES_PER_DAY)}'>"
        f"{trades_today} / {settings.MAX_TRADES_PER_DAY}</span></div>"
        f"<div class='sum-row'><span>Open positions</span>"
        f"<span class='{risk_cls(open_pos, settings.MAX_OPEN_POSITIONS)}'>"
        f"{open_pos} / {settings.MAX_OPEN_POSITIONS}</span></div>"
        f"<div class='sum-row'><span>Consecutive losses</span>"
        f"<span class='{consec_cls(consec)}'>{consec} / {settings.CONSECUTIVE_LOSSES_LIMIT}</span></div>"
        f"</div>"

        # ── Mode switches card ────────────────────────────────────────────────
        f"<div class='card'>"
        f"<h3>Mode</h3>"
        f"<div class='switch-row'><label>Trade Mode: {mode_badge}</label>"
        f"<form method='post' action='/trade-mode/toggle' style='margin:0'>"
        f"<button class='btn btn-gray' type='submit' style='width:auto;padding:8px 14px;"
        f"font-size:.88em'>Toggle</button></form></div>"
        f"<div class='switch-row'><label>Live/Paper: {paper_badge}</label>"
        f"<form method='post' action='/control/paper-mode/toggle' style='margin:0'>"
        f"<button class='btn {paper_btn_cls}' type='submit' style='width:auto;padding:8px 14px;"
        f"font-size:.88em'>{paper_btn_lbl}</button></form></div>"
        f"<form method='post' action='/control/emergency-stop/toggle' style='margin-top:8px'>"
        f"<button class='btn {estop_btn_cls}' type='submit'>{estop_btn_lbl}</button></form>"
        f"</div>"

        # ── Risk params card ──────────────────────────────────────────────────
        f"<div class='card'>"
        f"<h3>Risk Parameters</h3>"
        f"<form method='post' action='/control/risk'>"
        f"<div class='risk-row'><label>Max lots / trade{src('max_lots')}</label>"
        f"<input type='number' name='max_lots' value='{eff_max_lots}' min='1' max='20' step='1'>"
        f"<span class='unit'>lots</span></div>"
        f"<div class='risk-row'><label>Max daily loss{src('max_daily_loss')}</label>"
        f"<input type='number' name='max_daily_loss' value='{eff_max_loss:.0f}' min='500' step='500'>"
        f"<span class='unit'>₹</span></div>"
        f"<div class='risk-row'><label>SL % (options){src('sl_pct')}</label>"
        f"<input type='number' name='sl_pct' value='{eff_sl_pct*100:.1f}' min='1' max='50' step='0.5'>"
        f"<span class='unit'>%</span></div>"
        f"<div class='risk-row'><label>R:R ratio{src('rr_ratio')}</label>"
        f"<input type='number' name='rr_ratio' value='{eff_rr:.1f}' min='0.5' max='10' step='0.1'>"
        f"<span class='unit'>×</span></div>"
        f"<button class='apply-btn' type='submit'>Apply</button>"
        f"<button class='reset-btn' type='submit' name='reset' value='1'>Reset to defaults</button>"
        f"</form>"
        f"</div>"

        # ── Recent errors card ────────────────────────────────────────────────
        f"<div class='card'>"
        f"<h3>Recent Errors</h3>"
        f"<table><tr><th>Time</th><th>Type</th><th>Message</th></tr>{errors_html}</table>"
        f"</div>"

        f"</body></html>"
    )
    return Response(content=html, media_type="text/html")


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
) -> Response:
    rows = (
        session.query(Order, Position, Gtt, ClosedTrade)
        .outerjoin(Position, Position.order_id == Order.id)
        .outerjoin(Gtt, Gtt.order_id == Order.id)
        .outerjoin(ClosedTrade, ClosedTrade.position_id == Position.id)
        .order_by(Order.placed_at.desc())
        .limit(60)
        .all()
    )

    def pnl_cls(pnl):
        if pnl is None:
            return "color:#888"
        return "color:#28a745;font-weight:bold" if pnl >= 0 else "color:#dc3545;font-weight:bold"

    def status_badge(order, ct):
        if ct is not None:
            reason = ct.exit_reason or "CLOSED"
            bg = "#d4edda" if (ct.pnl or 0) >= 0 else "#f8d7da"
            return f"<span style='background:{bg};padding:2px 7px;border-radius:10px;font-size:.8em'>{reason}</span>"
        s = order.status
        if s == "DRY_RUN":
            return "<span style='background:#fff3cd;padding:2px 7px;border-radius:10px;font-size:.8em'>PAPER</span>"
        if s in ("PENDING", "COMPLETE"):
            return "<span style='background:#cce5ff;padding:2px 7px;border-radius:10px;font-size:.8em'>OPEN</span>"
        return f"<span style='background:#f0f0f0;padding:2px 7px;border-radius:10px;font-size:.8em'>{s}</span>"

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
            f"<td style='font-weight:500'>{order.tradingsymbol}</td>"
            f"<td>{order.transaction_type}</td>"
            f"<td style='text-align:right'>{lots}</td>"
            f"<td style='text-align:right'>{entry_px}</td>"
            f"<td style='text-align:right;color:#dc3545'>{sl}</td>"
            f"<td style='text-align:right;color:#28a745'>{tgt}</td>"
            f"<td style='text-align:right;{pnl_cls(pnl_val)}'>{pnl_str}</td>"
            f"<td>{status_badge(order, ct)}</td>"
            f"</tr>"
        )

    return Response(
        content=(
            _PAGE_HEAD + _NAV
            + "<h2>Orders</h2>"
            "<table>"
            "<tr><th>Time</th><th>Symbol</th><th>Side</th><th>Lots</th>"
            "<th>Entry</th><th>SL</th><th>Target</th><th>P&amp;L</th><th>Status</th></tr>"
            f"{rows_html}"
            "</table>"
            "</body></html>"
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
