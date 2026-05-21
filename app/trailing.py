from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

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
            new_sl = round(new_best * (1.0 - pos.sl_pct), 2)
            sl_improved = new_sl > pos.current_sl
        else:  # SELL (short): lower LTP is favorable
            if ltp >= pos.best_price:
                return
            new_best = ltp
            new_sl = round(new_best * (1.0 + pos.sl_pct), 2)
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
        from app.orders import modify_gtt
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

                if kite_gtt_id is not None:
                    modify_gtt(
                        kite,
                        kite_gtt_id,
                        sl_trigger=new_sl,
                        sl_limit=new_sl,
                        target_trigger=pos.target_price,
                        target_limit=pos.target_price,
                        last_price=ltp,
                        instrument=instrument,
                        qty=pos.qty,
                        product=pos.product,
                        entry_side=pos.entry_side,
                    )

                now_dt = datetime.now(_IST)
                db_gtt = session.query(Gtt).filter(Gtt.id == pos.gtt_db_id).first()
                if db_gtt:
                    db_gtt.sl_trigger = new_sl
                    db_gtt.sl_order_price = new_sl
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
            log.error(
                "TrailingSL: update failed for %s: %s", pos.tradingsymbol, exc, exc_info=True
            )
            self._revert(pos, old_sl)

    def _revert(self, pos: _TrailPos, old_sl: float) -> None:
        pos.current_sl = old_sl
        pos.last_update = 0.0  # allow retry on next tick
