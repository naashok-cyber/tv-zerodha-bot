from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.config import get_settings
from app.storage import ClosedTrade, Order, Position


class RiskHaltError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def compute_option_qty(
    capital_per_trade: Decimal,
    option_ltp: Decimal,
    lot_size: Decimal,
) -> int:
    """qty = floor(capital / ltp / lot_size) * lot_size; 0 if result < 1 lot."""
    if option_ltp <= 0 or lot_size <= 0:
        return 0
    lots = (capital_per_trade / option_ltp / lot_size).to_integral_value(rounding=ROUND_DOWN)
    qty = int(lots) * int(lot_size)
    return qty if qty >= int(lot_size) else 0


def compute_equity_qty(
    capital_per_trade: Decimal,
    price: Decimal,
) -> int:
    """qty = floor(capital / price); 0 if fewer than 1 share can be bought."""
    if price <= 0:
        return 0
    qty = int((capital_per_trade / price).to_integral_value(rounding=ROUND_DOWN))
    return qty if qty >= 1 else 0


def compute_futures_qty(
    capital_risk: Decimal,
    sl_distance: Decimal,
    lot_size: Decimal,
) -> int:
    """qty = floor(capital_risk / sl_distance / lot_size) * lot_size; 0 if < 1 lot."""
    if capital_risk <= 0 or sl_distance <= 0 or lot_size <= 0:
        return 0
    lots = (capital_risk / sl_distance / lot_size).to_integral_value(rounding=ROUND_DOWN)
    qty = int(lots) * int(lot_size)
    return qty if qty >= int(lot_size) else 0


def daily_loss_remaining(db: Any, today_ist: Any) -> Decimal:
    """Return remaining daily loss allowance; floored at 0. Injects today_ist — no internal clock."""
    settings = get_settings()
    total_capital = Decimal(str(settings.TOTAL_CAPITAL))
    max_daily_loss_pct_frac = Decimal(str(settings.MAX_DAILY_LOSS_PCT)) / Decimal("100")
    cap = min(settings.MAX_DAILY_LOSS, total_capital * max_daily_loss_pct_frac)

    today_start = today_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(ClosedTrade.pnl)
        .filter(ClosedTrade.closed_at >= today_start, ClosedTrade.pnl < 0)
        .all()
    )
    losses_abs = sum(abs(Decimal(str(r[0]))) for r in rows)
    remaining = cap - losses_abs
    return remaining if remaining > Decimal("0") else Decimal("0")


def check_risk_gates(db: Any, today_ist: Any) -> None:
    """Raise RiskHaltError if any gate is breached. Caller must skip this when DRY_RUN=True."""
    remaining = daily_loss_remaining(db, today_ist)
    if remaining == Decimal("0"):
        raise RiskHaltError("daily loss cap exhausted")

    rows = (
        db.query(ClosedTrade.pnl)
        .order_by(ClosedTrade.closed_at.desc())
        .all()
    )
    consecutive = 0
    for (pnl,) in rows:
        if Decimal(str(pnl)) < Decimal("0"):
            consecutive += 1
        else:
            break
    if consecutive >= 3:
        raise RiskHaltError(f"consecutive losing trades: {consecutive}")


def record_trade_result(
    db: Any,
    order_id: str,
    pnl: Decimal,
    closed_at_ist: Any,
) -> None:
    """Upsert a ClosedTrade row with the realized PnL for the entry order identified by order_id."""
    order = db.query(Order).filter(Order.kite_order_id == order_id).first()
    if order is None:
        return
    position = db.query(Position).filter(Position.order_id == order.id).first()
    if position is None:
        return
    ct = db.query(ClosedTrade).filter(ClosedTrade.position_id == position.id).first()
    if ct is not None:
        ct.pnl = float(pnl)
        ct.closed_at = closed_at_ist
    else:
        ct = ClosedTrade(
            position_id=position.id,
            exchange=position.exchange,
            tradingsymbol=position.tradingsymbol,
            entry_premium=position.entry_premium,
            exit_premium=0.0,
            pnl=float(pnl),
            exit_reason="GTT_FILLED",
            opened_at=position.opened_at,
            closed_at=closed_at_ist,
        )
        db.add(ct)
