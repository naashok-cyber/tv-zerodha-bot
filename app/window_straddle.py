"""Window-based short straddle: scheduled entries + timed exits.

Entry jobs fire via APScheduler cron at each window's open time.
Exit jobs fire via APScheduler cron at each window's close time.

Windows follow the backtest master recommendation table exactly
(5-min morning delay for NSE so the first bar is available):

  NIFTY      : 09:20-10:45 Mon-Thu        | 13:45-15:25 Tue,Wed        (monthly expiry only)
  BANKNIFTY  : 09:20-10:45 Mon-Thu        | 13:45-15:25 Wed,Thu
  NATURALGAS : 09:05-11:25 Mon-Wed,Fri   | 22:10-23:25 Tue,Fri
  CRUDEOILM  : 09:05-11:25 Mon-Wed,Fri   | 22:10-23:25 Mon-Wed,Fri

CRUDEOILM / NATURALGAS evening: if scheduled_straddle already placed a straddle
for the underlying today, the entry job skips (checked via open-position query).

Exit uses a LIMIT order at LTP + 0.5% to improve fill probability vs pure MARKET.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from app.config import IST

log = logging.getLogger(__name__)

# Weekday maps: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri
_DAY_NAMES = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
_TUE_ONLY        = [1]
_TUE_WED         = [1, 2]
_WED_THU         = [2, 3]
_MON_THU         = [0, 1, 2, 3]
_MON_TUE_WED_FRI = [0, 1, 2, 4]  # all days except Thursday
_TUE_FRI         = [1, 4]

# Config: underlying → {exchange, qty, monthly_only, windows: [(entry_hhmm, exit_hhmm, [weekdays])]}
WINDOW_STRADDLE_CFG: dict[str, dict] = {
    "NIFTY": {
        "exchange": "NFO",
        "qty": 2,
        "monthly_only": True,
        "windows": [
            ("09:20", "10:45", _MON_THU),
            ("13:45", "15:25", _TUE_WED),
        ],
    },
    "BANKNIFTY": {
        "exchange": "NFO",
        "qty": 1,
        "monthly_only": False,
        "windows": [
            ("09:20", "10:45", _MON_THU),
            ("13:45", "15:25", _WED_THU),
        ],
    },
    "NATURALGAS": {
        "exchange": "MCX",
        "qty": 2,
        "monthly_only": False,
        "windows": [
            ("09:05", "11:25", _MON_TUE_WED_FRI),
            ("22:10", "23:25", _TUE_FRI),
        ],
    },
}

_EXIT_SLIPPAGE = 0.005  # 0.5% above LTP for buy-back limit orders


def get_all_entry_jobs() -> list[dict]:
    """Return entry job specs for every configured window."""
    jobs: list[dict] = []
    for underlying, cfg in WINDOW_STRADDLE_CFG.items():
        for (entry_hhmm, exit_hhmm, allowed_days) in cfg["windows"]:
            h, m = entry_hhmm.split(":")
            dow = ",".join(_DAY_NAMES[d] for d in sorted(allowed_days))
            job_id = f"ws_entry_{underlying}_{entry_hhmm.replace(':', '')}"
            jobs.append({
                "underlying": underlying,
                "exchange": cfg["exchange"],
                "qty": cfg["qty"],
                "monthly_only": cfg.get("monthly_only", False),
                "entry_hhmm": entry_hhmm,
                "hour": int(h),
                "minute": int(m),
                "day_of_week": dow,
                "job_id": job_id,
            })
    return jobs


def get_all_exit_jobs() -> list[tuple[str, str, str, str]]:
    """Return (underlying, exchange, exit_hhmm, job_id) for every unique exit time."""
    jobs: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for underlying, cfg in WINDOW_STRADDLE_CFG.items():
        for (_, exit_hhmm, _) in cfg["windows"]:
            key = (underlying, exit_hhmm)
            if key not in seen:
                seen.add(key)
                job_id = f"ws_exit_{underlying}_{exit_hhmm.replace(':', '')}"
                jobs.append((underlying, cfg["exchange"], exit_hhmm, job_id))
    return jobs


# ── Entry job ──────────────────────────────────────────────────────────────────

def run_window_straddle_entry(
    underlying: str,
    exchange: str,
    qty: int,
    monthly_only: bool,
    entry_hhmm: str,
    settings: Any,
    session_factory: Any,
) -> None:
    """APScheduler entry job: validate + place short straddle for this instrument/window."""
    import app.state as state
    from app.kite_session import get_session_manager
    from app.storage import Alert
    from app.voice.straddle import validate_straddle

    if not state.is_window_straddle_enabled():
        log.debug("[ws_entry] %s %s: strategy disabled — skipping", underlying, entry_hhmm)
        return
    if state.get_session_invalid():
        log.warning("[ws_entry] %s %s: session invalid — skipping", underlying, entry_hhmm)
        return
    if state.is_emergency_stop():
        log.warning("[ws_entry] %s %s: emergency stop active — skipping", underlying, entry_hhmm)
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[ws_entry] %s: cannot get kite client: %s", underlying, exc)
        return

    now = datetime.now(IST)
    strategy_id = f"ws_{underlying}_{entry_hhmm.replace(':', '')}"

    with session_factory() as session:
        # Idempotency: only one entry per instrument+window per calendar day
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if (
            session.query(Alert)
            .filter(Alert.strategy_id == strategy_id, Alert.received_at >= today_start)
            .first()
        ):
            log.info(
                "[ws_entry] %s %s: already entered this window today — skipping",
                underlying, entry_hhmm,
            )
            return

        try:
            sv = validate_straddle(
                underlying, qty, kite, session, settings, now,
                exchange=exchange,
                monthly_only=monthly_only,
            )
        except Exception as exc:
            log.error("[ws_entry] %s: validate_straddle failed: %s", underlying, exc)
            return

        if not sv.margin_ok:
            log.error(
                "[ws_entry] %s: margin insufficient (%s) — skipping",
                underlying, sv.block_reason,
            )
            return

        if not sv.spread_ok:
            log.warning(
                "[ws_entry] %s: spread too wide (%s) — skipping",
                underlying, sv.block_reason,
            )
            return

        alert = Alert(
            received_at=now,
            strategy_id=strategy_id,
            tv_ticker=underlying,
            tv_exchange=exchange,
            action="STRADDLE_SHORT",
            order_type=None,
            entry_price=0.0,
            stop_loss=None,
            sl_percent=None,
            atr=None,
            quantity_hint=qty,
            product="NRML",
            tv_time=now.isoformat(),
            bar_time=None,
            interval="window_straddle",
            idempotency_key=str(uuid.uuid4()),
            raw_payload="{}",
            processed=False,
        )
        session.add(alert)
        session.commit()
        session.refresh(alert)

        entry_data = {
            "underlying": underlying,
            "exchange": exchange,
            "quantity": qty,
            "_straddle_ce_symbol": sv.ce.tradingsymbol,
            "_straddle_pe_symbol": sv.pe.tradingsymbol,
            "_straddle_atm_strike": sv.atm_strike,
            "_straddle_expiry": str(sv.expiry),
            "_straddle_lot_size": sv.lot_size,
            "_straddle_lot_units": sv.lot_units,
            "_straddle_net_credit": sv.net_credit_per_lot,
            "_straddle_est_sl": sv.estimated_sl_per_lot,
            "_straddle_all_ok": sv.all_ok,
            "_straddle_force_spread": False,
        }

    log.info(
        "[ws_entry] %s %s: alert=%d CE=%s PE=%s qty=%d credit=%.2f+%.2f expiry=%s",
        underlying, entry_hhmm, alert.id,
        sv.ce.tradingsymbol, sv.pe.tradingsymbol, qty,
        sv.ce.ltp, sv.pe.ltp, sv.expiry,
    )

    from app.main import _process_straddle
    _process_straddle(alert.id, entry_data, settings)


# ── Exit job ───────────────────────────────────────────────────────────────────

def squareoff_window_straddle(
    underlying: str,
    exchange: str,
    settings: Any,
    session_factory: Any,
) -> None:
    """APScheduler exit job: close all open straddle positions for this underlying.

    Places LIMIT buy-back orders at LTP + 0.5% for better fill probability.
    Falls back to MARKET if LTP cannot be fetched.
    Skips silently when window straddle strategy is disabled.
    """
    import app.state as state
    from app.kite_session import get_session_manager
    from app.orders import cancel_gtt, square_off
    from app.storage import ClosedTrade, Gtt, Instrument, Order, Position

    if not state.is_window_straddle_enabled():
        log.debug("[ws_exit] %s: strategy disabled — skipping exit job", underlying)
        return
    if state.get_session_invalid():
        log.warning("[ws_exit] %s: session invalid — skipping", underlying)
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[ws_exit] %s: cannot get kite client: %s", underlying, exc)
        return

    now = datetime.now(IST)
    target_exchanges = ["MCX", "MCX-OPT"] if exchange == "MCX" else ["NSE", "NFO"]

    with session_factory() as session:
        open_positions = (
            session.query(Position)
            .join(Order, Order.id == Position.order_id)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.underlying == underlying,
                Position.exchange.in_(target_exchanges),
                Position.instrument_type.in_(["CE", "PE"]),
                Order.straddle_id.isnot(None),
                ClosedTrade.id == None,  # noqa: E711
                # Paper straddles are closed at LTP by the paper monitor.
                Order.dry_run == False,  # noqa: E712
            )
            .all()
        )

        if not open_positions:
            log.info("[ws_exit] %s: no open straddle positions — nothing to do", underlying)
            return

        log.info(
            "[ws_exit] %s: closing %d position(s): %s",
            underlying, len(open_positions),
            [p.tradingsymbol for p in open_positions],
        )

        for position in open_positions:
            try:
                gtt = (
                    session.query(Gtt)
                    .filter(Gtt.order_id == position.order_id, Gtt.status == "ACTIVE")
                    .first()
                )
                if gtt and gtt.kite_gtt_id:
                    try:
                        cancel_gtt(kite, gtt.kite_gtt_id)
                    except Exception as exc:
                        log.warning(
                            "[ws_exit] %s: cancel GTT %d failed: %s",
                            underlying, gtt.kite_gtt_id, exc,
                        )
                    gtt.status = "CANCELLED"
                    gtt.updated_at = now

                try:
                    from app.main import _trailing_manager
                    if _trailing_manager is not None:
                        _trailing_manager.unregister(position.tradingsymbol)
                except Exception:
                    pass

                entry_order = session.query(Order).filter(Order.id == position.order_id).first()
                instrument = (
                    session.query(Instrument)
                    .filter(
                        Instrument.tradingsymbol == position.tradingsymbol,
                        Instrument.exchange == position.exchange,
                    )
                    .first()
                )
                if instrument is None:
                    log.error(
                        "[ws_exit] %s: instrument not found for %s — skipping",
                        underlying, position.tradingsymbol,
                    )
                    continue

                # Fetch LTP and compute limit exit price (LTP + 0.5% for buy-back fill probability)
                limit_px: float | None = None
                try:
                    q_key = f"{position.exchange}:{position.tradingsymbol}"
                    q = kite.quote([q_key])
                    ltp = float(q.get(q_key, {}).get("last_price", 0.0))
                    if ltp > 0:
                        from app.symbol_mapper import round_to_tick
                        tick = float(instrument.tick_size) if instrument.tick_size else 0.05
                        limit_px = round_to_tick(ltp * (1 + _EXIT_SLIPPAGE), tick)
                        log.info(
                            "[ws_exit] %s: %s LTP=%.2f limit=%.2f (+%.1f%%)",
                            underlying, position.tradingsymbol, ltp, limit_px,
                            _EXIT_SLIPPAGE * 100,
                        )
                except Exception as exc:
                    log.warning(
                        "[ws_exit] %s: LTP fetch failed for %s — using MARKET: %s",
                        underlying, position.tradingsymbol, exc,
                    )

                product = entry_order.product if entry_order else "NRML"
                entry_side = entry_order.transaction_type if entry_order else "SELL"

                sq_id = square_off(
                    kite, instrument, position.quantity,
                    product=product, entry_side=entry_side,
                    limit_price=limit_px,
                )
                from app.storage import booked_partial_pnl, trade_meta_for_order
                _ws_sid, _ws_dry = trade_meta_for_order(session, entry_order)
                ct = ClosedTrade(
                    position_id=position.id,
                    exchange=position.exchange,
                    tradingsymbol=position.tradingsymbol,
                    entry_premium=position.entry_premium,
                    exit_premium=0.0,
                    pnl=booked_partial_pnl(position),
                    exit_reason="window_straddle_exit",
                    opened_at=position.opened_at,
                    closed_at=now,
                    strategy_id=_ws_sid,
                    dry_run=_ws_dry,
                )
                session.add(ct)
                log.info(
                    "[ws_exit] %s: closed %s qty=%d sq_order=%s",
                    underlying, position.tradingsymbol, position.quantity, sq_id,
                )
            except Exception as exc:
                log.error(
                    "[ws_exit] %s: squareoff failed for %s: %s",
                    underlying, position.tradingsymbol, exc,
                    exc_info=True,
                )

        session.commit()
