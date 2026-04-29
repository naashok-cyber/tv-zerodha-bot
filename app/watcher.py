from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class EntryFilledEvent:
    kite_order_id: str
    fill_price: float
    fill_qty: int
    data: dict = field(default_factory=dict)


@dataclass
class GttFilledEvent:
    kite_order_id: str
    tradingsymbol: str
    fill_price: float
    fill_qty: int
    transaction_type: str   # "SELL" for long exits, "BUY" for short covers
    data: dict = field(default_factory=dict)


class OrderWatcher:
    """Wraps KiteTicker to watch for order postbacks and tick data.

    Inject a ticker_factory to avoid real websocket connections in tests.
    Inject on_entry_filled to handle entry fills without coupling to orders.py.
    Inject on_gtt_filled to handle GTT exit fills.
    """

    def __init__(
        self,
        on_entry_filled: Callable[[EntryFilledEvent], None] | None = None,
        on_gtt_filled: Callable[[GttFilledEvent], None] | None = None,
        ticker_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._on_entry_filled = on_entry_filled
        self._on_gtt_filled = on_gtt_filled
        self._ticker_factory = ticker_factory or _default_ticker_factory
        self._ticker: Any = None
        self._subscribed_tokens: set[int] = set()
        self._watched_order_ids: set[str] = set()
        self._started: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, api_key: str, access_token: str) -> None:
        if self._started:
            log.warning("OrderWatcher.start() called but already started; ignoring")
            return
        self._started = True
        self._ticker = self._ticker_factory(api_key=api_key, access_token=access_token)
        self._ticker.on_connect = self.on_connect
        self._ticker.on_close = self.on_close
        self._ticker.on_error = self.on_error
        self._ticker.on_reconnect = self.on_reconnect
        self._ticker.on_noreconnect = self.on_noreconnect
        self._ticker.on_order_update = self.on_order_update
        self._ticker.on_ticks = self.on_ticks
        self._ticker.connect(threaded=True)

    def restart(self, api_key: str, access_token: str) -> None:
        """Stop any existing ticker and start fresh with new credentials.

        Call this after a fresh Kite OAuth login so the watcher picks up the
        new access token even if it previously gave up reconnecting.
        """
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception:
                pass
            self._ticker = None
        self._started = False
        self.start(api_key=api_key, access_token=access_token)

    # ── Entry order tracking ──────────────────────────────────────────────────

    def watch_order(self, kite_order_id: str) -> None:
        self._watched_order_ids.add(kite_order_id)

    def unwatch_order(self, kite_order_id: str) -> None:
        self._watched_order_ids.discard(kite_order_id)

    # ── Subscription management ───────────────────────────────────────────────

    def subscribe(self, instrument_tokens: list[int]) -> None:
        self._subscribed_tokens.update(instrument_tokens)
        if self._ticker is not None:
            self._ticker.subscribe(instrument_tokens)

    def unsubscribe(self, instrument_tokens: list[int]) -> None:
        for token in instrument_tokens:
            self._subscribed_tokens.discard(token)
        if self._ticker is not None:
            self._ticker.unsubscribe(instrument_tokens)

    # ── KiteTicker callbacks ──────────────────────────────────────────────────

    def on_connect(self, ws: Any, response: Any) -> None:
        log.info("Ticker connected")
        if self._subscribed_tokens:
            self._ticker.subscribe(list(self._subscribed_tokens))

    def on_close(self, ws: Any, code: Any, reason: Any) -> None:
        log.info("Ticker closed: code=%s reason=%s", code, reason)

    def on_error(self, ws: Any, code: Any, reason: Any) -> None:
        log.error("Ticker error: code=%s reason=%s", code, reason)

    def on_reconnect(self, ws: Any, attempts_count: int) -> None:
        log.warning("Ticker reconnecting, attempt %d", attempts_count)
        if self._subscribed_tokens and self._ticker is not None:
            self._ticker.subscribe(list(self._subscribed_tokens))

    def on_noreconnect(self, ws: Any) -> None:
        log.error("Ticker gave up reconnecting")
        self._started = False  # allow restart() after a fresh login

    def on_order_update(self, ws: Any, data: dict) -> None:
        order_id = str(data.get("order_id", ""))
        status = data.get("status", "")
        log.info("Order update: id=%s status=%s", order_id, status)

        if status == "COMPLETE" and order_id in self._watched_order_ids:
            self._watched_order_ids.discard(order_id)
            event = EntryFilledEvent(
                kite_order_id=order_id,
                fill_price=float(data.get("average_price", 0.0)),
                fill_qty=int(data.get("filled_quantity", 0)),
                data=data,
            )
            if self._on_entry_filled is not None:
                self._on_entry_filled(event)
        elif status == "COMPLETE" and data.get("transaction_type") == "SELL":
            # GTT exit fill — any COMPLETE SELL not tracked as an entry
            event = GttFilledEvent(
                kite_order_id=order_id,
                tradingsymbol=str(data.get("tradingsymbol", "")),
                fill_price=float(data.get("average_price", 0.0)),
                fill_qty=int(data.get("filled_quantity", 0)),
                transaction_type="SELL",
                data=data,
            )
            if self._on_gtt_filled is not None:
                self._on_gtt_filled(event)

    def on_ticks(self, ws: Any, ticks: list[dict]) -> None:
        log.debug("Received %d ticks (MTM stub)", len(ticks))


def _default_ticker_factory(api_key: str, access_token: str) -> Any:
    from kiteconnect import KiteTicker
    return KiteTicker(api_key=api_key, access_token=access_token)
