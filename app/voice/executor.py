"""Bridge between voice confirm and the main trading pipeline.

Uses late imports (inside function bodies) to break the circular dependency
that would arise if app.main were imported at module level here, since
app.main imports app.routes.voice at module level via include_router.

For BUY/SELL: creates an Alert row + queues _process_alert as a background task
  (same path as a TradingView webhook, tagged with source="voice_manual" in strategy_id).
For EXIT_ALL/SQUARE_OFF: mirrors _process_alert's EXIT branch directly, filtering
  by Position.underlying so all open positions for that underlying are exited.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)


def execute_voice_entry(
    order: dict,
    conf_token: str,
    background_tasks: Any,
    db: Any,
    settings: Any,
) -> tuple[dict, int]:
    """Create an Alert row and queue it through _process_alert (background)."""
    from app.main import _process_alert  # late import — avoids circular dep
    from app.schemas import AlertPayload
    from app.storage import Alert
    from app.config import IST

    now = datetime.now(IST)
    ikey = str(uuid.uuid4())

    alert = Alert(
        received_at=now,
        strategy_id=f"voice_{conf_token[:8]}",
        tv_ticker=order["underlying"],
        tv_exchange=order.get("exchange", "NFO"),
        action=order["action"],          # "BUY" or "SELL"
        order_type=None,
        entry_price=0.0,
        stop_loss=None,
        sl_percent=None,
        atr=None,
        quantity_hint=int(order.get("quantity", 1)),
        product="NRML",
        tv_time=now.isoformat(),
        bar_time=None,
        interval="voice",
        idempotency_key=ikey,
        raw_payload=json.dumps(order),
        processed=False,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    limit_price_val = order.get("limit_price")
    _strike_val = order.get("_strike")
    payload = AlertPayload(
        symbol=order["underlying"],
        action=order["action"],
        instrument_type="OPTIONS",
        price=Decimal("0.01"),  # placeholder; not used in options strike selection
        limit_price=Decimal(str(limit_price_val)) if limit_price_val else None,
        option_type=order.get("_option_type") or None,
        strike=float(_strike_val) if _strike_val is not None else None,
        timeframe="voice",
        alert_id=ikey,
        timestamp=now,
    )

    background_tasks.add_task(_process_alert, alert.id, payload, settings)
    log.info(
        "voice_entry queued: token=%s alert_id=%d underlying=%s action=%s",
        conf_token[:8], alert.id, order["underlying"], order["action"],
    )
    return {"status": "queued", "alert_id": alert.id, "source": "voice_manual"}, 202


def execute_voice_straddle(
    order: dict,
    conf_token: str,
    background_tasks: Any,
    db: Any,
    settings: Any,
) -> tuple[dict, int]:
    """Queue a short straddle through _process_straddle (background)."""
    from app.main import _process_straddle  # late import — avoids circular dep
    from app.storage import Alert
    from app.config import IST

    now = datetime.now(IST)
    ikey = str(uuid.uuid4())

    underlying = order.get("underlying", "NATURALGAS")
    alert = Alert(
        received_at=now,
        strategy_id=f"straddle_{conf_token[:8]}",
        tv_ticker=underlying,
        tv_exchange=order.get("exchange", "MCX"),
        action="STRADDLE_SHORT",
        order_type=None,
        entry_price=0.0,
        stop_loss=None,
        sl_percent=None,
        atr=None,
        quantity_hint=int(order.get("quantity", 1)),
        product="NRML",
        tv_time=now.isoformat(),
        bar_time=None,
        interval="voice",
        idempotency_key=ikey,
        raw_payload=json.dumps({k: v for k, v in order.items() if not k.startswith("_")}),
        processed=False,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    background_tasks.add_task(_process_straddle, alert.id, dict(order), settings)
    log.info(
        "straddle queued: token=%s alert_id=%d underlying=%s qty=%d",
        conf_token[:8], alert.id, underlying, int(order.get("quantity", 1)),
    )
    return {"status": "queued", "alert_id": alert.id, "source": "straddle_short"}, 202


def execute_voice_exit(
    underlying: str,
    db: Any,
    settings: Any,
) -> tuple[dict, int]:
    """Exit all open positions for `underlying`.

    Mirrors _process_alert's EXIT branch but filters by Position.underlying
    (not tradingsymbol) so options positions are correctly found.
    """
    from app.storage import ClosedTrade, Gtt, Instrument, Order, Position
    from app.orders import cancel_gtt, square_off
    from app.kite_session import get_session_manager
    from app.config import IST
    import app.state as state

    now = datetime.now(IST)
    product = settings.PRODUCT_TYPE.value
    dry_run = state.is_paper_mode(settings.DRY_RUN)

    # Scope to the active mode: a paper exit must never DB-close a live
    # position (its GTT would stay armed at Kite while we think it's flat).
    open_positions = (
        db.query(Position)
        .join(Order, Position.order_id == Order.id)
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(
            Position.underlying == underlying,
            ClosedTrade.id == None,  # noqa: E711
            Order.dry_run == dry_run,
        )
        .all()
    )

    if not open_positions:
        return {"error": f"No open positions found for {underlying}"}, 404

    if not dry_run:
        kite = get_session_manager().get_kite()
    else:
        # Paper exits mark to LTP so the simulated trade closes with a real PnL.
        try:
            kite = get_session_manager().get_kite()
        except Exception:
            kite = None
    exited = []

    for position in open_positions:
        gtt = (
            db.query(Gtt)
            .filter(Gtt.order_id == position.order_id, Gtt.status != "CANCELLED")
            .first()
        )
        instrument = (
            db.query(Instrument)
            .filter(
                Instrument.tradingsymbol == position.tradingsymbol,
                Instrument.exchange == position.exchange,
            )
            .first()
        )
        entry_order = db.query(Order).filter(Order.id == position.order_id).first()
        exit_product = entry_order.product if entry_order else product

        if not dry_run and kite is not None:
            if gtt and gtt.kite_gtt_id:
                try:
                    cancel_gtt(kite, gtt.kite_gtt_id)
                except Exception as exc:
                    log.error("voice_exit: cancel_gtt failed for %s: %s", position.tradingsymbol, exc)
            if gtt:
                gtt.status = "CANCELLED"
            if instrument:
                try:
                    square_off(kite, instrument, position.quantity, exit_product)
                except Exception as exc:
                    log.error("voice_exit: square_off failed for %s: %s", position.tradingsymbol, exc)
        else:
            log.info(
                "voice_exit DRY_RUN — EXIT %s qty=%d product=%s at LTP",
                position.tradingsymbol, position.quantity, exit_product,
            )
            if gtt:
                gtt.status = "CANCELLED"

        _exit_premium = 0.0
        _exit_pnl = 0.0
        if dry_run and kite is not None:
            try:
                from app.paper_trading import paper_pnl
                _q_key = f"{position.exchange}:{position.tradingsymbol}"
                _exit_premium = float(kite.ltp([_q_key])[_q_key]["last_price"])
                _exit_pnl = paper_pnl(
                    entry_order.transaction_type if entry_order else "BUY",
                    position.entry_premium, _exit_premium, position.quantity,
                    position.exchange, position.underlying or position.tradingsymbol,
                    settings,
                )
            except Exception as exc:
                log.warning("voice_exit: paper LTP unavailable for %s (%s) — recording pnl=0",
                            position.tradingsymbol, exc)

        from app.storage import trade_meta_for_order
        _vx_sid, _vx_dry = trade_meta_for_order(db, entry_order)
        ct = ClosedTrade(
            position_id=position.id,
            exchange=position.exchange,
            tradingsymbol=position.tradingsymbol,
            entry_premium=position.entry_premium,
            exit_premium=_exit_premium,
            pnl=_exit_pnl,
            exit_reason="VOICE_MANUAL_EXIT",
            opened_at=position.opened_at,
            closed_at=now,
            strategy_id=_vx_sid,
            dry_run=_vx_dry,
        )
        db.add(ct)
        exited.append(position.tradingsymbol)

    db.commit()
    log.info("voice_exit: exited %d position(s) for %s: %s", len(exited), underlying, exited)
    return {
        "status": "exited",
        "underlying": underlying,
        "positions_closed": exited,
        "count": len(exited),
        "dry_run": dry_run,
    }, 200
