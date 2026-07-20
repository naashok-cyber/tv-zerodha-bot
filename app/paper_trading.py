"""Paper-trade lifecycle: simulated entry fills and simulated GTT OCO exits.

In paper mode (DRY_RUN or the /control paper toggle) entries run the full real
pipeline — strike selection, sizing, SL/target computation — but instead of a
Kite order the caller synthesizes an EntryFilledEvent at LTP with a PAPER-
prefixed order id.  _on_entry_filled then persists the Position and a Gtt row
with status=DRY_RUN exactly as it would for a live fill.

This module owns the exit side: paper_monitor_job runs every minute during
market hours, quotes LTP for all open paper positions, and closes them when
the simulated GTT band (sl_trigger / target_trigger) is crossed — or at the
same end-of-day squareoff times the live scheduler uses.  Closes are written
as ClosedTrade rows with dry_run=True so they never pollute live analytics or
risk guards, and live squareoff/hedge jobs must never touch paper positions.

Known simplifications vs live execution:
- fills at LTP (no spread/slippage model)
- SL is static (no trailing; live trailing needs GTT modify calls)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from app.config import IST
from app.kite_session import get_session_manager
from app.storage import (
    ClosedTrade,
    Gtt,
    Order,
    Position,
    trade_meta_for_order,
)

log = logging.getLogger(__name__)

PAPER_ORDER_PREFIX = "PAPER-"


def new_paper_order_id() -> str:
    """Synthetic kite_order_id for a simulated fill; namespaced to never collide."""
    return PAPER_ORDER_PREFIX + uuid.uuid4().hex[:12]


def is_paper_order_id(order_id: str | None) -> bool:
    return bool(order_id) and str(order_id).startswith(PAPER_ORDER_PREFIX)


def paper_pnl(
    entry_side: str,
    entry_premium: float,
    exit_premium: float,
    qty: int,
    exchange: str,
    underlying: str,
    settings: Any,
) -> float:
    """INR PnL using the same units logic as _on_gtt_filled (MCX lots vs units)."""
    mcx_units = (
        settings.MCX_LOT_UNITS.get(underlying, 1) if exchange == "MCX" else 1
    )
    diff = exit_premium - entry_premium
    if entry_side == "SELL":
        diff = -diff
    return diff * qty * mcx_units


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def _past(now: datetime, hhmm: str) -> bool:
    h, m = _parse_hhmm(hhmm)
    return (now.hour, now.minute) >= (h, m)


def close_paper_position(
    session: Any,
    position: Position,
    order: Order,
    gtt: Gtt | None,
    exit_premium: float,
    exit_reason: str,
    now: datetime,
    settings: Any,
) -> ClosedTrade:
    """Write the dry_run ClosedTrade for a paper position and retire its Gtt row."""
    entry_side = order.transaction_type or "BUY"
    pnl = paper_pnl(
        entry_side, position.entry_premium, exit_premium,
        position.quantity, position.exchange, position.underlying or position.tradingsymbol,
        settings,
    )
    strategy_id, _ = trade_meta_for_order(session, order)
    from app.storage import booked_partial_pnl
    ct = ClosedTrade(
        position_id=position.id,
        exchange=position.exchange,
        tradingsymbol=position.tradingsymbol,
        entry_premium=position.entry_premium,
        exit_premium=exit_premium,
        pnl=pnl + booked_partial_pnl(position),
        exit_reason=exit_reason,
        opened_at=position.opened_at,
        closed_at=now,
        strategy_id=strategy_id,
        dry_run=True,
    )
    session.add(ct)
    if gtt is not None:
        gtt.status = "TRIGGERED" if exit_reason in ("SL_HIT", "TARGET_HIT") else "CANCELLED"
        gtt.updated_at = now
    log.info(
        "[paper] closed %s %s qty=%d entry=%.4f exit=%.4f pnl=%.2f (%s)",
        entry_side, position.tradingsymbol, position.quantity,
        position.entry_premium, exit_premium, pnl, exit_reason,
    )
    return ct


def _open_paper_rows(session: Any) -> list[tuple[Position, Order, Gtt | None]]:
    rows = (
        session.query(Position, Order, Gtt)
        .join(Order, Position.order_id == Order.id)
        .outerjoin(Gtt, Position.gtt_id == Gtt.id)
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(
            Order.dry_run == True,  # noqa: E712
            ClosedTrade.id == None,  # noqa: E711
        )
        .all()
    )
    return [(p, o, g) for p, o, g in rows]


def _eod_exit_reason(position: Position, strategy_id: str | None, now: datetime, settings: Any) -> str | None:
    """Return the squareoff reason if this paper position is past its EOD close time.

    Mirrors the live scheduler: scheduled straddles at STRADDLE_SQUAREOFF_TIME,
    MCX at MCX_SQUAREOFF_TIME, NSE/NFO at NSE_SQUAREOFF_TIME.
    """
    if strategy_id and strategy_id.startswith("sched_straddle") and _past(now, settings.STRADDLE_SQUAREOFF_TIME):
        return "scheduled_straddle_squareoff"
    if position.exchange in ("MCX", "MCX-OPT") and _past(now, settings.MCX_SQUAREOFF_TIME):
        return "EOD_MCX"
    if position.exchange in ("NSE", "NFO") and _past(now, settings.NSE_SQUAREOFF_TIME):
        return "EOD_NSE"
    return None


def paper_monitor_job(settings: Any, session_factory: Any) -> None:
    """1-min cron: simulate GTT OCO exits + EOD squareoff for open paper positions."""
    with session_factory() as session:
        open_rows = _open_paper_rows(session)
        if not open_rows:
            return

        try:
            kite = get_session_manager().get_kite()
        except Exception as exc:
            log.debug("[paper] monitor skipped — no Kite session for quotes: %s", exc)
            return

        keys = sorted({f"{p.exchange}:{p.tradingsymbol}" for p, _, _ in open_rows})
        try:
            quotes = kite.ltp(keys)
        except Exception as exc:
            log.warning("[paper] monitor: LTP fetch failed for %d symbols: %s", len(keys), exc)
            return

        now = datetime.now(IST)
        closed_position_ids: set[int] = set()
        # (straddle_id, trigger position id) for paired-leg exits after the main loop
        paired_exits: list[str] = []

        for position, order, gtt in open_rows:
            if position.id in closed_position_ids:
                continue
            key = f"{position.exchange}:{position.tradingsymbol}"
            ltp_raw = quotes.get(key, {}).get("last_price")
            if ltp_raw is None:
                log.warning("[paper] monitor: no LTP for %s — skipping", key)
                continue
            ltp = float(ltp_raw)
            entry_side = order.transaction_type or "BUY"
            strategy_id, _ = trade_meta_for_order(session, order)

            exit_reason: str | None = None
            if gtt is not None and gtt.sl_trigger is not None and gtt.target_trigger is not None:
                if entry_side == "SELL":
                    if ltp >= gtt.sl_trigger:
                        exit_reason = "SL_HIT"
                    elif ltp <= gtt.target_trigger:
                        exit_reason = "TARGET_HIT"
                else:
                    if ltp <= gtt.sl_trigger:
                        exit_reason = "SL_HIT"
                    elif ltp >= gtt.target_trigger:
                        exit_reason = "TARGET_HIT"

            if exit_reason is None:
                exit_reason = _eod_exit_reason(position, strategy_id, now, settings)
            if exit_reason is None:
                continue

            close_paper_position(session, position, order, gtt, ltp, exit_reason, now, settings)
            closed_position_ids.add(position.id)
            # Live behavior: when one straddle leg's GTT fills, the sibling is
            # squared off immediately (_on_gtt_filled paired-leg exit).
            if order.straddle_id and exit_reason in ("SL_HIT", "TARGET_HIT"):
                paired_exits.append(order.straddle_id)

        for straddle_id in paired_exits:
            for position, order, gtt in open_rows:
                if position.id in closed_position_ids or order.straddle_id != straddle_id:
                    continue
                key = f"{position.exchange}:{position.tradingsymbol}"
                ltp_raw = quotes.get(key, {}).get("last_price")
                if ltp_raw is None:
                    continue
                close_paper_position(
                    session, position, order, gtt, float(ltp_raw),
                    "straddle_paired_sl_exit", now, settings,
                )
                closed_position_ids.add(position.id)

        if closed_position_ids:
            session.commit()
