from __future__ import annotations

import logging
import random
import time
from decimal import Decimal
from typing import Any

from kiteconnect.exceptions import InputException, NetworkException, TokenException

from app.storage import Instrument
from app.symbol_mapper import round_to_tick

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_CAP_DELAY = 8.0


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

    order_dict = [
        {
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "transaction_type": exit_side,
            "quantity": qty,
            "order_type": "LIMIT",
            "product": product,
            "price": sl_limit_r,
        },
        {
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "transaction_type": exit_side,
            "quantity": qty,
            "order_type": "LIMIT",
            "product": product,
            "price": target_limit_r,
        },
    ]

    gtt_id: int = backoff_call(
        kite_client.place_gtt,
        trigger_type=kite_client.GTT_TYPE_OCO,
        tradingsymbol=instrument.tradingsymbol,
        exchange=instrument.exchange,
        trigger_values=[sl_trigger_r, target_trigger_r],
        last_price=last_price_r,
        orders=order_dict,
    )
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
    exit_side: str = "SELL",
) -> int | None:
    """Modify an existing GTT OCO order in place.

    Uses the same OCO condition structure as place_gtt_oco.
    Returns gtt_id on success.
    """
    order_dict = [
        {
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "transaction_type": exit_side,
            "quantity": qty,
            "order_type": "LIMIT",
            "product": product,
            "price": sl_limit,
        },
        {
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "transaction_type": exit_side,
            "quantity": qty,
            "order_type": "LIMIT",
            "product": product,
            "price": target_limit,
        },
    ]
    result: int = backoff_call(
        kite_client.modify_gtt,
        trigger_id=gtt_id,
        trigger_type=kite_client.GTT_TYPE_OCO,
        tradingsymbol=instrument.tradingsymbol,
        exchange=instrument.exchange,
        trigger_values=[sl_trigger, target_trigger],
        last_price=last_price,
        orders=order_dict,
    )
    log.info("Modified GTT %d for %s: sl=%.4f target=%.4f", gtt_id, instrument.tradingsymbol, sl_trigger, target_trigger)
    return result


def cancel_gtt(kite_client: Any, gtt_id: int) -> bool:
    """Cancel a GTT by id. Returns True on success."""
    backoff_call(kite_client.delete_gtt, gtt_id)
    log.info("Cancelled GTT %d", gtt_id)
    return True


def square_off(
    kite_client: Any,
    instrument: Instrument,
    qty: int,
    product: str,
    entry_side: str = "BUY",
    variety: str = "regular",
) -> str | None:
    """Place a MARKET order opposite to entry_side to close a position."""
    exit_side = "SELL" if entry_side.upper() == "BUY" else "BUY"

    params: dict[str, Any] = {
        "variety": variety,
        "exchange": instrument.exchange,
        "tradingsymbol": instrument.tradingsymbol,
        "transaction_type": exit_side,
        "quantity": qty,
        "order_type": "MARKET",
        "product": product,
        "market_protection": -1,
    }

    order_id: str = backoff_call(kite_client.place_order, **params)
    log.info(
        "Square-off order %s: %s %d %s MARKET",
        order_id, exit_side, qty, instrument.tradingsymbol,
    )
    return order_id
