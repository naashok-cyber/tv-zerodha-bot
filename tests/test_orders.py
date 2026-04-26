"""Tests for app/orders.py — all offline; KiteConnect fully mocked."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest
from kiteconnect.exceptions import DataException, InputException, NetworkException

from app.orders import backoff_call, cancel_gtt, place_entry, place_gtt_oco, square_off
from app.storage import Instrument


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _instrument(
    tradingsymbol: str = "NIFTY2541722750CE",
    exchange: str = "NFO",
    tick_size: float = 0.05,
    lot_size: int = 75,
) -> Instrument:
    inst = Instrument()
    inst.instrument_token = 1234
    inst.exchange_token = 5678
    inst.tradingsymbol = tradingsymbol
    inst.name = "NIFTY"
    inst.tick_size = tick_size
    inst.lot_size = lot_size
    inst.instrument_type = "CE"
    inst.segment = "NFO-OPT"
    inst.exchange = exchange
    inst.expiry = None
    inst.strike = 22750.0
    return inst


def _kite(order_id: str = "order123") -> MagicMock:
    kite = MagicMock()
    kite.place_order.return_value = order_id
    kite.GTT_TYPE_OCO = "two-leg"
    return kite


# ── market_protection presence ────────────────────────────────────────────────

def test_market_buy_has_market_protection() -> None:
    kite = _kite()
    place_entry(kite, _instrument(), side="BUY", qty=75, order_type="MARKET", product="NRML")
    _, kwargs = kite.place_order.call_args
    assert kwargs["market_protection"] == -1


def test_market_sell_has_market_protection() -> None:
    kite = _kite()
    place_entry(kite, _instrument(), side="SELL", qty=75, order_type="MARKET", product="NRML")
    _, kwargs = kite.place_order.call_args
    assert kwargs["market_protection"] == -1


def test_slm_has_market_protection() -> None:
    kite = _kite()
    place_entry(
        kite, _instrument(), side="BUY", qty=75, order_type="SL-M", product="NRML",
        trigger_price=Decimal("100.00"),
    )
    _, kwargs = kite.place_order.call_args
    assert kwargs["market_protection"] == -1


def test_limit_order_no_market_protection() -> None:
    kite = _kite()
    place_entry(
        kite, _instrument(), side="BUY", qty=75, order_type="LIMIT", product="NRML",
        price=Decimal("100.05"),
    )
    _, kwargs = kite.place_order.call_args
    assert "market_protection" not in kwargs


# ── Tick-size rounding ────────────────────────────────────────────────────────

def test_tick_rounding_standard() -> None:
    # 100.034 with tick 0.05: 100.034 / 0.05 = 2000.68 → rounds up → 2001 × 0.05 = 100.05
    kite = _kite()
    place_entry(
        kite, _instrument(tick_size=0.05), side="BUY", qty=75, order_type="LIMIT",
        product="NRML", price=Decimal("100.034"),
    )
    _, kwargs = kite.place_order.call_args
    assert kwargs["price"] == pytest.approx(100.05)


def test_tick_rounding_half_tick_boundary() -> None:
    # 100.025 is exactly half a tick above 100.00; ROUND_HALF_UP → 100.05
    kite = _kite()
    place_entry(
        kite, _instrument(tick_size=0.05), side="BUY", qty=75, order_type="LIMIT",
        product="NRML", price=Decimal("100.025"),
    )
    _, kwargs = kite.place_order.call_args
    assert kwargs["price"] == pytest.approx(100.05)


# ── Lot-size validation ───────────────────────────────────────────────────────

def test_lot_size_rejection_raises() -> None:
    kite = _kite()
    with pytest.raises(ValueError, match="lot_size"):
        place_entry(
            kite, _instrument(lot_size=75), side="BUY", qty=50,
            order_type="MARKET", product="NRML",
        )
    kite.place_order.assert_not_called()


# ── GTT OCO leg construction ──────────────────────────────────────────────────

def test_gtt_oco_buy_entry_produces_two_sell_legs() -> None:
    kite = _kite()
    kite.place_gtt.return_value = 999
    inst = _instrument()

    place_gtt_oco(
        kite, inst, qty=75,
        sl_trigger=Decimal("70.00"),
        sl_limit=Decimal("70.00"),
        target_trigger=Decimal("130.00"),
        target_limit=Decimal("130.00"),
        last_price=Decimal("100.00"),
        product="NRML",
        entry_side="BUY",
    )

    _, kwargs = kite.place_gtt.call_args
    orders = kwargs["orders"]
    assert len(orders) == 2
    assert orders[0]["transaction_type"] == "SELL"   # SL leg
    assert orders[1]["transaction_type"] == "SELL"   # target leg
    assert orders[0]["price"] == pytest.approx(70.00)
    assert orders[1]["price"] == pytest.approx(130.00)
    assert kwargs["trigger_values"] == pytest.approx([70.00, 130.00])
    assert kwargs["trigger_type"] == "two-leg"
    assert kwargs["last_price"] == pytest.approx(100.00)


def test_gtt_oco_sell_entry_produces_two_buy_legs() -> None:
    kite = _kite()
    kite.place_gtt.return_value = 888

    place_gtt_oco(
        kite, _instrument(), qty=75,
        sl_trigger=Decimal("130.00"),
        sl_limit=Decimal("130.00"),
        target_trigger=Decimal("70.00"),
        target_limit=Decimal("70.00"),
        last_price=Decimal("100.00"),
        product="NRML",
        entry_side="SELL",
    )

    _, kwargs = kite.place_gtt.call_args
    orders = kwargs["orders"]
    assert orders[0]["transaction_type"] == "BUY"
    assert orders[1]["transaction_type"] == "BUY"


# ── cancel_gtt ────────────────────────────────────────────────────────────────

def test_cancel_gtt_calls_delete_gtt() -> None:
    kite = MagicMock()
    result = cancel_gtt(kite, gtt_id=42)
    kite.delete_gtt.assert_called_once_with(42)
    assert result is True


# ── square_off ────────────────────────────────────────────────────────────────

def test_square_off_market_sell_with_market_protection() -> None:
    kite = _kite("sqoff456")
    result = square_off(kite, _instrument(), qty=75, product="NRML", entry_side="BUY")
    _, kwargs = kite.place_order.call_args
    assert kwargs["order_type"] == "MARKET"
    assert kwargs["transaction_type"] == "SELL"
    assert kwargs["market_protection"] == -1
    assert result == "sqoff456"


# ── Backoff behaviour ─────────────────────────────────────────────────────────

def test_backoff_retries_on_network_exception() -> None:
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise NetworkException("timeout")
        return "ok"

    with patch("app.orders.time.sleep"):
        result = backoff_call(flaky, _max_retries=3, _base_delay=0.0)

    assert result == "ok"
    assert len(attempts) == 3


def test_backoff_retries_on_429() -> None:
    attempts = []

    def rate_limited(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise DataException("too many requests", 429)
        return "ok"

    with patch("app.orders.time.sleep"):
        result = backoff_call(rate_limited, _max_retries=3, _base_delay=0.0)

    assert result == "ok"
    assert len(attempts) == 2


def test_backoff_no_retry_on_input_exception() -> None:
    calls = []

    def bad_input(*args, **kwargs):
        calls.append(1)
        raise InputException("invalid param")

    with patch("app.orders.time.sleep"):
        with pytest.raises(InputException):
            backoff_call(bad_input, _max_retries=3)

    assert len(calls) == 1
