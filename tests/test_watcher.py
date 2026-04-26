"""Tests for app/watcher.py — all offline; KiteTicker fully mocked."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.watcher import EntryFilledEvent, OrderWatcher


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_watcher(on_entry_filled=None):
    """Return an OrderWatcher with a mock ticker already attached via start()."""
    mock_ticker = MagicMock()
    factory = MagicMock(return_value=mock_ticker)
    watcher = OrderWatcher(on_entry_filled=on_entry_filled, ticker_factory=factory)
    watcher.start(api_key="fake_key", access_token="fake_token")
    return watcher, mock_ticker


def _order_update(order_id: str, status: str, fill_price: float = 105.0, fill_qty: int = 75) -> dict:
    return {
        "order_id": order_id,
        "status": status,
        "average_price": fill_price,
        "filled_quantity": fill_qty,
    }


# ── on_order_update ───────────────────────────────────────────────────────────

def test_complete_entry_fires_entry_filled_event() -> None:
    fired: list[EntryFilledEvent] = []
    watcher, ticker = _make_watcher(on_entry_filled=fired.append)

    watcher.watch_order("ORD001")
    watcher.on_order_update(ticker, _order_update("ORD001", "COMPLETE", fill_price=105.0, fill_qty=75))

    assert len(fired) == 1
    evt = fired[0]
    assert evt.kite_order_id == "ORD001"
    assert evt.fill_price == pytest.approx(105.0)
    assert evt.fill_qty == 75


def test_rejected_entry_does_not_fire_event() -> None:
    fired: list[EntryFilledEvent] = []
    watcher, ticker = _make_watcher(on_entry_filled=fired.append)

    watcher.watch_order("ORD002")
    watcher.on_order_update(ticker, _order_update("ORD002", "REJECTED"))

    assert len(fired) == 0


def test_complete_unwatched_order_does_not_fire_event() -> None:
    fired: list[EntryFilledEvent] = []
    watcher, ticker = _make_watcher(on_entry_filled=fired.append)

    # ORD003 was never added via watch_order
    watcher.on_order_update(ticker, _order_update("ORD003", "COMPLETE"))

    assert len(fired) == 0


# ── subscribe / unsubscribe ───────────────────────────────────────────────────

def test_subscribe_passes_tokens_to_ticker() -> None:
    watcher, ticker = _make_watcher()
    watcher.subscribe([111, 222])
    ticker.subscribe.assert_called_with([111, 222])


def test_unsubscribe_passes_tokens_to_ticker() -> None:
    watcher, ticker = _make_watcher()
    watcher.subscribe([111, 222])
    ticker.subscribe.reset_mock()

    watcher.unsubscribe([111])
    ticker.unsubscribe.assert_called_with([111])
    assert 111 not in watcher._subscribed_tokens
    assert 222 in watcher._subscribed_tokens


# ── on_reconnect ──────────────────────────────────────────────────────────────

def test_reconnect_resubscribes_active_tokens() -> None:
    watcher, ticker = _make_watcher()
    watcher.subscribe([333, 444])
    ticker.subscribe.reset_mock()

    watcher.on_reconnect(ticker, attempts_count=1)

    called_tokens = set(ticker.subscribe.call_args[0][0])
    assert called_tokens == {333, 444}


# ── on_ticks stub ─────────────────────────────────────────────────────────────

def test_on_ticks_stub_does_not_raise() -> None:
    watcher, ticker = _make_watcher()
    sample_ticks = [{"instrument_token": 111, "last_price": 100.5}]
    watcher.on_ticks(ticker, sample_ticks)   # must not raise
