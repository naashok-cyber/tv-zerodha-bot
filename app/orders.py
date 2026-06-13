from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any

from kiteconnect.exceptions import InputException, NetworkException, TokenException

from app.storage import Instrument
from app.symbol_mapper import round_to_tick

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_CAP_DELAY = 8.0

# Per-symbol mutex + recent-squareoff TTL set. Together they prevent two threads
# (e.g. straddle_squareoff cron and eod_squareoff cron) from placing duplicate
# closing orders for the same position within the same Python process.
_squareoff_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_recent_squareoffs: dict[str, float] = {}
_RECENT_SQUAREOFF_TTL_SEC = 30.0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, NetworkException):
        return True
    # kiteconnect raises any KiteException subclass with .code; 429 = rate limit
    if hasattr(exc, "code") and exc.code == 429:
        return True
    return False


def backoff_call(
    fn: Any,
    *args: Any,
    _max_retries: int = _MAX_RETRIES,
    _base_delay: float = _BASE_DELAY,
    _cap: float = _CAP_DELAY,
    **kwargs: Any,
) -> Any:
    """Call fn(*args, **kwargs) with exponential backoff on retryable errors.

    Raises immediately on InputException or TokenException.
    Retries up to _max_retries times on NetworkException or HTTP 429.
    """
    for attempt in range(_max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except (InputException, TokenException):
            raise
        except BaseException as exc:
            if _is_retryable(exc):
                if attempt == _max_retries:
                    raise
                delay = min(_cap, _base_delay * (2 ** attempt)) + random.uniform(0, 0.1)
                log.warning(
                    "Attempt %d failed (%s); retrying in %.2fs", attempt + 1, exc, delay
                )
                time.sleep(delay)
            else:
                raise
    return None  # unreachable; satisfies mypy


def place_entry(
    kite_client: Any,
    instrument: Instrument,
    side: str,
    qty: int,
    order_type: str,
    product: str,
    *,
    price: Decimal | None = None,
    trigger_price: Decimal | None = None,
    variety: str = "regular",
) -> str | None:
    """Place an entry order and return the kite_order_id.

    Raises ValueError if qty is not a multiple of instrument.lot_size.
    """
    if qty % instrument.lot_size != 0:
        raise ValueError(
            f"qty {qty} is not a multiple of lot_size {instrument.lot_size} "
            f"for {instrument.tradingsymbol}"
        )

    tick = Decimal(str(instrument.tick_size))

    params: dict[str, Any] = {
        "variety": variety,
        "exchange": instrument.exchange,
        "tradingsymbol": instrument.tradingsymbol,
        "transaction_type": side,
        "quantity": qty,
        "order_type": order_type,
        "product": product,
    }

    if order_type in ("MARKET", "SL-M"):
        params["market_protection"] = -1

    if order_type in ("LIMIT", "SL") and price is not None:
        params["price"] = float(round_to_tick(price, tick))

    if order_type in ("SL-M", "SL") and trigger_price is not None:
        params["trigger_price"] = float(round_to_tick(trigger_price, tick))

    order_id: str = backoff_call(kite_client.place_order, **params)
    log.info(
        "Placed entry order %s: %s %s qty=%d type=%s",
        order_id, side, instrument.tradingsymbol, qty, order_type,
    )
    return order_id


def compute_oco_limits(
    sl_trigger: Decimal | float,
    target_trigger: Decimal | float,
    entry_side: str,
    buffer_pct: float,
) -> tuple[Decimal, Decimal]:
    """Compute GTT OCO leg limit prices, offset from triggers by buffer_pct so
    orders fill on gap moves instead of getting stuck at the exact trigger.

    For SELL entry (legs are BUY-to-close):  limit = trigger × (1 + buffer)
    For BUY  entry (legs are SELL-to-close): limit = trigger × (1 − buffer)
    Same direction for SL and target legs (both more aggressive than trigger).
    Caller (place_gtt_oco / modify_gtt) rounds to tick.
    """
    sl_d = Decimal(str(sl_trigger))
    tgt_d = Decimal(str(target_trigger))
    buf = Decimal(str(buffer_pct))
    factor = (Decimal("1") + buf) if entry_side.upper() == "SELL" else (Decimal("1") - buf)
    return sl_d * factor, tgt_d * factor


def place_gtt_oco(
    kite_client: Any,
    instrument: Instrument,
    qty: int,
    sl_trigger: Decimal,
    sl_limit: Decimal,
    target_trigger: Decimal,
    target_limit: Decimal,
    last_price: Decimal,
    product: str,
    entry_side: str = "BUY",
) -> int | None:
    """Place a GTT OCO order: SL as leg 0, target as leg 1.

    Both exit legs are the opposite transaction type to entry_side.
    Returns the kite_gtt_id.
    """
    tick = Decimal(str(instrument.tick_size))
    exit_side = "SELL" if entry_side.upper() == "BUY" else "BUY"

    sl_trigger_r = float(round_to_tick(sl_trigger, tick))
    sl_limit_r = float(round_to_tick(sl_limit, tick))
    target_trigger_r = float(round_to_tick(target_trigger, tick))
    target_limit_r = float(round_to_tick(target_limit, tick))
    last_price_r = float(round_to_tick(last_price, tick))

    def _leg(price: float) -> dict:
        return {
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "transaction_type": exit_side,
            "quantity": qty,
            "order_type": "LIMIT",
            "product": product,
            "price": price,
        }

    sl_leg = _leg(sl_limit_r)
    tgt_leg = _leg(target_limit_r)

    if entry_side.upper() == "BUY":
        # Long: SL trigger is below last_price, target trigger is above — Kite needs [low, high]
        trigger_values = [sl_trigger_r, target_trigger_r]
        orders = [sl_leg, tgt_leg]
    else:
        # Short: target trigger is below last_price (profit when premium falls),
        # SL trigger is above (loss when premium rises) — still pass [low, high] to Kite
        trigger_values = [target_trigger_r, sl_trigger_r]
        orders = [tgt_leg, sl_leg]

    raw = backoff_call(
        kite_client.place_gtt,
        trigger_type=kite_client.GTT_TYPE_OCO,
        tradingsymbol=instrument.tradingsymbol,
        exchange=instrument.exchange,
        trigger_values=trigger_values,
        last_price=last_price_r,
        orders=orders,
    )
    # Kite returns either an int or {"trigger_id": <int>} depending on SDK version.
    gtt_id: int = raw["trigger_id"] if isinstance(raw, dict) else int(raw)
    log.info("Placed GTT OCO %d for %s", gtt_id, instrument.tradingsymbol)
    return gtt_id


def modify_gtt(
    kite_client: Any,
    gtt_id: int,
    sl_trigger: float,
    sl_limit: float,
    target_trigger: float,
    target_limit: float,
    last_price: float,
    instrument: Instrument,
    qty: int,
    product: str,
    entry_side: str = "BUY",
) -> int | None:
    """Modify an existing GTT OCO order in place.

    Uses the same OCO condition structure as place_gtt_oco:
    trigger_values must be [low, high] for Kite, regardless of which is SL vs target.
    Returns gtt_id on success.
    """
    exit_side = "SELL" if entry_side.upper() == "BUY" else "BUY"

    def _leg(price: float) -> dict:
        return {
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "transaction_type": exit_side,
            "quantity": qty,
            "order_type": "LIMIT",
            "product": product,
            "price": price,
        }

    sl_leg = _leg(sl_limit)
    tgt_leg = _leg(target_limit)

    if entry_side.upper() == "BUY":
        trigger_values = [sl_trigger, target_trigger]   # [low, high]
        orders = [sl_leg, tgt_leg]
    else:
        trigger_values = [target_trigger, sl_trigger]   # [low, high] for SELL
        orders = [tgt_leg, sl_leg]

    result: int = backoff_call(
        kite_client.modify_gtt,
        trigger_id=gtt_id,
        trigger_type=kite_client.GTT_TYPE_OCO,
        tradingsymbol=instrument.tradingsymbol,
        exchange=instrument.exchange,
        trigger_values=trigger_values,
        last_price=last_price,
        orders=orders,
    )
    log.info("Modified GTT %d for %s: sl=%.4f target=%.4f", gtt_id, instrument.tradingsymbol, sl_trigger, target_trigger)
    return result


def cancel_gtt(kite_client: Any, gtt_id: int) -> bool:
    """Cancel a GTT by id. Returns True on success."""
    backoff_call(kite_client.delete_gtt, gtt_id)
    log.info("Cancelled GTT %d", gtt_id)
    return True


def is_position_open(
    kite_client: Any, tradingsymbol: str, exchange: str
) -> tuple[bool, int]:
    """Return (is_open, net_quantity) per Kite's positions() net array.

    On API failure returns (True, 0) so callers proceed — safer for EOD where
    leaving a stale position open overnight is worse than risking a no-op order.
    """
    try:
        positions = kite_client.positions()
    except Exception as exc:
        log.warning(
            "is_position_open: kite.positions() failed for %s — assuming open: %s",
            tradingsymbol, exc,
        )
        return (True, 0)

    for p in positions.get("net", []) or []:
        if p.get("tradingsymbol") == tradingsymbol and p.get("exchange") == exchange:
            qty = int(p.get("quantity", 0))
            return (qty != 0, qty)
    return (False, 0)


def square_off(
    kite_client: Any,
    instrument: Instrument,
    qty: int,
    product: str,
    entry_side: str = "BUY",
    variety: str = "regular",
    limit_price: float | None = None,
) -> str | None:
    """Place an order opposite to entry_side to close a position.

    limit_price: when provided, places a LIMIT order at that price instead of MARKET.
    Guards against duplicate closes:
      1. Per-symbol process lock — serializes concurrent calls for the same symbol
      2. Recent-squareoff TTL — same process won't re-close within 30s
      3. Kite positions() check — skips if broker reports the position already flat
         (handles manual user closes, prior GTT triggers, and races where step 2
         already placed a close).
    """
    exit_side = "SELL" if entry_side.upper() == "BUY" else "BUY"
    symbol = instrument.tradingsymbol

    with _squareoff_locks[symbol]:
        now_mono = time.monotonic()
        last = _recent_squareoffs.get(symbol)
        if last is not None and (now_mono - last) < _RECENT_SQUAREOFF_TTL_SEC:
            log.info(
                "Square-off skipped: %s already closed %.1fs ago in this process",
                symbol, now_mono - last,
            )
            return None

        is_open, kite_qty = is_position_open(
            kite_client, symbol, instrument.exchange
        )
        if not is_open:
            log.info(
                "Square-off skipped: %s flat per Kite (net qty=%d) — no order placed",
                symbol, kite_qty,
            )
            _recent_squareoffs[symbol] = now_mono
            return None

        if limit_price is not None:
            params: dict[str, Any] = {
                "variety": variety,
                "exchange": instrument.exchange,
                "tradingsymbol": symbol,
                "transaction_type": exit_side,
                "quantity": qty,
                "order_type": "LIMIT",
                "price": limit_price,
                "product": product,
            }
        else:
            params = {
                "variety": variety,
                "exchange": instrument.exchange,
                "tradingsymbol": symbol,
                "transaction_type": exit_side,
                "quantity": qty,
                "order_type": "MARKET",
                "product": product,
                "market_protection": -1,
            }

        order_id: str = backoff_call(kite_client.place_order, **params)
        _recent_squareoffs[symbol] = now_mono
        log.info(
            "Square-off order %s: %s %d %s %s (kite_net_qty=%d)",
            order_id, exit_side, qty, symbol,
            f"LIMIT@{limit_price}" if limit_price is not None else "MARKET",
            kite_qty,
        )
        return order_id
