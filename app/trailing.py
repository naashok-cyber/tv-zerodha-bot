from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo


def _round_to_tick(price: float, tick: float) -> float:
    """Round price to nearest valid tick boundary (handles MCX 0.05/0.10 ticks)."""
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 2)

log = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


@dataclass
class _TrailPos:
    tradingsymbol: str
    instrument_token: int
    exchange: str
    entry_side: str        # "BUY" or "SELL"
    sl_pct: float          # e.g. 0.15
    qty: int
    product: str
    target_price: float    # fixed profit target — never changes
    current_sl: float      # best SL seen so far; improves as price moves favorably
    best_price: float      # best LTP seen since entry; drives when to trail
    gtt_db_id: int         # DB Gtt.id
    kite_gtt_id: int | None
    tick_size: float = 0.01  # minimum price increment; rounds the trailed SL
    last_update: float = field(default_factory=time.monotonic)


class TrailingSlManager:
    """Watches live ticks and updates the GTT stop-loss whenever LTP moves
    favorably by any amount (at most one GTT modify per 30 s per position).
    The profit target is never changed.
    """

    UPDATE_INTERVAL = 30.0  # seconds between GTT modify calls per position

    def __init__(
        self,
        session_factory: Any,
        kite_fetcher: Callable[[], Any],
    ) -> None:
        self._session_factory = session_factory
        self._kite_fetcher = kite_fetcher
        self._positions: dict[int, _TrailPos] = {}  # keyed by instrument_token
        self._lock = threading.Lock()

    # ── Registration ─────────────────────────────────────────────────────────

    def register(
        self,
        *,
        tradingsymbol: str,
        instrument_token: int,
        exchange: str,
        entry_side: str,
        sl_pct: float,
        qty: int,
        product: str,
        target_price: float,
        initial_sl: float,
        fill_price: float,
        gtt_db_id: int,
        kite_gtt_id: int | None,
        tick_size: float = 0.01,
    ) -> None:
        pos = _TrailPos(
            tradingsymbol=tradingsymbol,
            instrument_token=instrument_token,
            exchange=exchange,
            entry_side=entry_side,
            sl_pct=sl_pct,
            qty=qty,
            product=product,
            target_price=target_price,
            current_sl=initial_sl,
            best_price=fill_price,
            gtt_db_id=gtt_db_id,
            kite_gtt_id=kite_gtt_id,
            tick_size=tick_size,
        )
        with self._lock:
            self._positions[instrument_token] = pos
        log.info(
            "TrailingSL registered: %s token=%d %s sl_pct=%.0f%% sl=%.4f target=%.4f",
            tradingsymbol, instrument_token, entry_side, sl_pct * 100,
            initial_sl, target_price,
        )

    def unregister(self, instrument_token: int) -> None:
        with self._lock:
            pos = self._positions.pop(instrument_token, None)
        if pos:
            log.info("TrailingSL unregistered: %s (token %d)", pos.tradingsymbol, instrument_token)

    def unregister_by_symbol(self, tradingsymbol: str) -> None:
        with self._lock:
            token = next(
                (t for t, p in self._positions.items() if p.tradingsymbol == tradingsymbol),
                None,
            )
            if token is not None:
                del self._positions[token]
                log.info("TrailingSL unregistered: %s", tradingsymbol)

    # ── Tick processing ───────────────────────────────────────────────────────

    def on_ticks(self, ticks: list[dict]) -> None:
        for tick in ticks:
            token = tick.get("instrument_token")
            ltp = tick.get("last_price")
            if token is None or ltp is None:
                continue
            with self._lock:
                pos = self._positions.get(token)
            if pos is None:
                continue
            self._evaluate(pos, float(ltp))

    def _evaluate(self, pos: _TrailPos, ltp: float) -> None:
        """Check if the trailing SL should be moved; spawn an update thread if so."""
        if pos.entry_side == "BUY":
            if ltp <= pos.best_price:
                return
            new_best = ltp
            new_sl = _round_to_tick(new_best * (1.0 - pos.sl_pct), pos.tick_size)
            sl_improved = new_sl > pos.current_sl
        else:  # SELL (short): lower LTP is favorable
            if ltp >= pos.best_price:
                return
            new_best = ltp
            new_sl = _round_to_tick(new_best * (1.0 + pos.sl_pct), pos.tick_size)
            sl_improved = new_sl < pos.current_sl

        pos.best_price = new_best

        if not sl_improved:
            return

        # Throttle: at most one GTT modify per UPDATE_INTERVAL
        now = time.monotonic()
        if now - pos.last_update < self.UPDATE_INTERVAL:
            return

        # Ensure the GTT band is still valid at current LTP before modifying
        if pos.entry_side == "SELL":
            if not (pos.target_price < ltp < new_sl):
                return
        else:
            if not (new_sl < ltp < pos.target_price):
                return

        old_sl = pos.current_sl
        pos.current_sl = new_sl
        pos.last_update = now

        threading.Thread(
            target=self._do_update,
            args=(pos, old_sl, new_sl, pos.kite_gtt_id, ltp),
            daemon=True,
        ).start()

    def _do_update(
        self,
        pos: _TrailPos,
        old_sl: float,
        new_sl: float,
        kite_gtt_id: int | None,
        ltp: float,
    ) -> None:
        from app.config import get_settings
        from app.orders import compute_oco_limits, modify_gtt
        from app.storage import Position as DbPosition, Gtt, Instrument

        try:
            kite = self._kite_fetcher()
            with self._session_factory() as session:
                instrument = (
                    session.query(Instrument)
                    .filter(
                        Instrument.instrument_token == pos.instrument_token,
                        Instrument.exchange == pos.exchange,
                    )
                    .first()
                )
                if instrument is None:
                    log.error("TrailingSL: instrument not found token=%d", pos.instrument_token)
                    self._revert(pos, old_sl)
                    return

                now_dt = datetime.now(_IST)
                db_gtt = session.query(Gtt).filter(Gtt.id == pos.gtt_db_id).first()
                if db_gtt is None or db_gtt.status != "ACTIVE":
                    log.info(
                        "TrailingSL: %s GTT no longer active (status=%s), unregistering",
                        pos.tradingsymbol,
                        db_gtt.status if db_gtt else "NOT_FOUND",
                    )
                    self.unregister(pos.instrument_token)
                    return

                buffer_pct = float(get_settings().OCO_SLIPPAGE_BUFFER_PCT)
                sl_limit_d, tgt_limit_d = compute_oco_limits(
                    new_sl, pos.target_price, pos.entry_side, buffer_pct,
                )
                sl_limit_r = _round_to_tick(float(sl_limit_d), pos.tick_size)
                tgt_limit_r = _round_to_tick(float(tgt_limit_d), pos.tick_size)

                if kite_gtt_id is not None:
                    modify_gtt(
                        kite,
                        kite_gtt_id,
                        sl_trigger=new_sl,
                        sl_limit=sl_limit_r,
                        target_trigger=pos.target_price,
                        target_limit=tgt_limit_r,
                        last_price=ltp,
                        instrument=instrument,
                        qty=pos.qty,
                        product=pos.product,
                        entry_side=pos.entry_side,
                    )

                if db_gtt:
                    db_gtt.sl_trigger = new_sl
                    db_gtt.sl_order_price = sl_limit_r
                    db_gtt.target_order_price = tgt_limit_r
                    db_gtt.modification_count += 1
                    db_gtt.updated_at = now_dt

                db_pos = session.query(DbPosition).filter(DbPosition.gtt_id == pos.gtt_db_id).first()
                if db_pos:
                    db_pos.current_sl = new_sl
                    db_pos.trail_active = True
                    db_pos.last_updated_at = now_dt

                session.commit()
                log.info(
                    "TrailingSL: %s SL %.4f → %.4f (LTP %.4f best %.4f)",
                    pos.tradingsymbol, old_sl, new_sl, ltp, pos.best_price,
                )
        except Exception as exc:
            from kiteconnect.exceptions import InputException
            if isinstance(exc, InputException) and "invalid trigger" in str(exc).lower():
                # GTT was deleted/triggered on the broker side — stop retrying
                log.error(
                    "TrailingSL: GTT %s gone from Kite for %s (Invalid trigger ID) — "
                    "marking KITE_INVALID and unregistering",
                    kite_gtt_id, pos.tradingsymbol,
                )
                try:
                    from app.storage import Gtt
                    with self._session_factory() as _s:
                        _g = _s.query(Gtt).filter(Gtt.id == pos.gtt_db_id).first()
                        if _g:
                            _g.status = "KITE_INVALID"
                            _s.commit()
                except Exception as _db_exc:
                    log.error("TrailingSL: failed to mark GTT dead in DB: %s", _db_exc)
                self.unregister(pos.instrument_token)
            else:
                log.error(
                    "TrailingSL: update failed for %s: %s", pos.tradingsymbol, exc, exc_info=True
                )
                self._revert(pos, old_sl)

    def _revert(self, pos: _TrailPos, old_sl: float) -> None:
        pos.current_sl = old_sl
        pos.last_update = time.monotonic()  # respect UPDATE_INTERVAL before retrying
