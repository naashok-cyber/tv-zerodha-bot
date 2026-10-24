"""Supertrend direction + the skewed landing of the band hedge's lot solve.

The trigger band is untouched by this feature — only *where* δ lands after a
rebalance changes: just past zero on the Supertrend side, instead of whichever
side whole-lot rounding happened to pick.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import hedge_core
from app.config import Settings
from app.delta_hedge import _build_specs
from app.hedge_core import _adx_regime_mult, _skewed_lots, _supertrend_dir
from app.supertrend import compute_supertrend

NG_LOT = -625.0   # SELL 1 ATM NG lot: −0.5 δ × 1250 mmBtu


@pytest.fixture
def cfg() -> Settings:
    return Settings(
        SECRET_KEY="x" * 32,
        KITE_API_KEY="k",
        KITE_API_SECRET="s",
        VOICE_AUTH_TOKEN="v",
        NG_DELTA_HEDGE_ENABLED=True,
        CRUDEOILM_HEDGE_ENABLED=True,
    )


def _candles(closes: list[float], spread: float = 1.0) -> list[dict]:
    return [{"open": c, "high": c + spread, "low": c - spread, "close": c} for c in closes]


# ── Supertrend ───────────────────────────────────────────────────────────────

def test_supertrend_up_on_a_rising_tape():
    assert compute_supertrend(_candles([100 + i for i in range(40)])) == 1


def test_supertrend_down_on_a_falling_tape():
    assert compute_supertrend(_candles([200 - i for i in range(40)])) == -1


def test_supertrend_flips_after_a_reversal():
    up_then_down = [100 + i for i in range(30)] + [129 - 2 * i for i in range(20)]
    assert compute_supertrend(_candles(up_then_down)) == -1


def test_supertrend_holds_through_a_small_pullback():
    """A dip shallower than k·ATR must not flip it — that is the whole filter."""
    closes = [100 + i for i in range(30)] + [128.5, 128.0, 129.0]
    assert compute_supertrend(_candles(closes)) == 1


def test_supertrend_needs_enough_candles():
    assert compute_supertrend(_candles([100.0] * 11), period=10) is None
    assert compute_supertrend([], period=10) is None


# ── Skewed lot solve ─────────────────────────────────────────────────────────

def test_long_breach_lands_positive_in_an_uptrend():
    """δ=+2000, ST up: 3 lots → +125 (stays long, with the trend)."""
    lots, skewed = _skewed_lots(2000, NG_LOT, 6, +1, 400)
    assert (lots, skewed) == (3, True)
    assert 2000 + lots * NG_LOT == pytest.approx(125)


def test_long_breach_lands_negative_in_a_downtrend_when_within_cap():
    """δ=+1500, ST down: 3 lots → −375 (≤ 400 cap, so trend side wins)."""
    lots, skewed = _skewed_lots(1500, NG_LOT, 6, -1, 400)
    assert (lots, skewed) == (3, True)
    assert 1500 + lots * NG_LOT == pytest.approx(-375)


def test_trend_side_beyond_cap_falls_back_to_nearest_zero():
    """δ=+2000, ST down: the trend-side rung is −500 > 400 → take +125."""
    lots, skewed = _skewed_lots(2000, NG_LOT, 6, -1, 400)
    assert (lots, skewed) == (3, False)
    # ...and a wider cap lets the trend side through.
    assert _skewed_lots(2000, NG_LOT, 6, -1, 500) == (4, True)


def test_short_breach_is_the_mirror_image():
    """δ=−2000 is hedged with positive-δ lots (SELL PE adds +625)."""
    assert _skewed_lots(-2000, 625, 6, -1, 400) == (3, True)    # → −125
    assert _skewed_lots(-1500, 625, 6, +1, 400) == (3, True)    # → +375
    assert _skewed_lots(-2000, 625, 6, +1, 400) == (3, False)   # +500 > cap → −125


def test_sign_follows_supertrend_not_rounding():
    """The bug being fixed: consecutive rebalances must not alternate sign
    while the trend holds. With a cap of one lot's δ a trend-side rung always
    exists; at 400 some breaches (e.g. +1800 → +550) must fall back instead."""
    for delta in (1300, 1500, 1800, 2000, 2400):
        lots, skewed = _skewed_lots(delta, NG_LOT, 6, +1, 625)
        assert skewed and delta + lots * NG_LOT >= 0, delta
    assert _skewed_lots(1800, NG_LOT, 6, +1, 400) == (3, False)   # → −75


def test_skew_respects_max_lots():
    assert _skewed_lots(10_000, NG_LOT, 6, +1, 400) == (6, False)


def test_crude_lands_tightly_on_the_trend_side():
    """Crude's ~5 bbl lots leave a trend-side rung in (0, 5] every time."""
    lots, skewed = _skewed_lots(23, -5.0, 6, +1, 8)
    assert (lots, skewed) == (4, True)          # → +3 bbl
    lots, skewed = _skewed_lots(23, -5.0, 6, -1, 8)
    assert (lots, skewed) == (5, True)          # → −2 bbl


# ── Supertrend rides the ADX fetch ───────────────────────────────────────────

def _spec(cfg):
    return _build_specs(cfg)[0]


def test_supertrend_cached_from_the_adx_candles(cfg, monkeypatch):
    monkeypatch.setattr(hedge_core, "_ADX_CACHE", {})
    monkeypatch.setattr(hedge_core, "_ST_CACHE", {})
    kite = MagicMock()
    kite.historical_data.return_value = _candles([300 + i for i in range(60)])
    spec = _spec(cfg)
    fut = SimpleNamespace(instrument_token=1)

    _adx_regime_mult(kite, fut, spec, cfg)
    assert _supertrend_dir(spec) == 1
    _adx_regime_mult(kite, fut, spec, cfg)     # ADX cache hit
    assert kite.historical_data.call_count == 1, "Supertrend must not add a fetch"
    assert _supertrend_dir(spec) == 1


def test_failed_fetch_clears_the_skew(cfg, monkeypatch):
    monkeypatch.setattr(hedge_core, "_ADX_CACHE", {})
    monkeypatch.setattr(hedge_core, "_ST_CACHE", {_spec(cfg).name: (1e18, 1)})
    kite = MagicMock()
    kite.historical_data.side_effect = RuntimeError("kite down")
    _adx_regime_mult(kite, SimpleNamespace(instrument_token=1), _spec(cfg), cfg)
    assert _supertrend_dir(_spec(cfg)) is None


def test_stale_direction_is_ignored(cfg, monkeypatch):
    monkeypatch.setattr(hedge_core, "_ST_CACHE", {_spec(cfg).name: (-1e9, 1)})
    assert _supertrend_dir(_spec(cfg)) is None


# ── Spec wiring ──────────────────────────────────────────────────────────────

def test_specs_carry_their_skew_caps(cfg):
    ng, crude = _build_specs(cfg)
    assert ng.skew_max_units == pytest.approx(400.0)      # mmBtu
    assert crude.skew_max_units == pytest.approx(8.0)     # bbl


def test_skew_cap_zero_turns_it_off(cfg):
    off = cfg.model_copy(update={"NG_HEDGE_ST_SKEW_MAX_UNITS": 0.0})
    assert _build_specs(off)[0].skew_max_units == 0.0
