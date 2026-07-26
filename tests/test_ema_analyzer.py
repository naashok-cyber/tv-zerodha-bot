"""Tests for app/ema_analyzer.py signal engine — all pure, no network/DB."""
from __future__ import annotations

from app.ema_analyzer import AnalyzerSignal, compute_signal, ema_series


def _candles(closes: list[float]) -> list[dict]:
    return [{"close": c} for c in closes]


def _first_cross_up_index(closes: list[float], fast: int, slow: int) -> int | None:
    """Index of the first bar where the fast EMA crosses above the slow EMA."""
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    for i in range(1, len(closes)):
        if None in (ef[i - 1], es[i - 1], ef[i], es[i]):
            continue
        if ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
            return i
    return None


def _first_cross_dn_index(closes: list[float], fast: int, slow: int) -> int | None:
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    for i in range(1, len(closes)):
        if None in (ef[i - 1], es[i - 1], ef[i], es[i]):
            continue
        if ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
            return i
    return None


# ── ema_series ─────────────────────────────────────────────────────────────────

def test_ema_series_seeds_with_sma_then_smooths():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    out = ema_series(values, 3)
    # Warm-up bars are undefined until the SMA seed at index period-1.
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0             # SMA(1,2,3)
    assert out[3] == 3.0             # 4*0.5 + 2*0.5
    assert out[4] == 4.0             # 5*0.5 + 3*0.5
    assert out[9] == 9.0


def test_ema_series_insufficient_returns_all_none():
    assert ema_series([1, 2], 5) == [None, None]


# ── compute_signal: gating ──────────────────────────────────────────────────────

def test_insufficient_candles_returns_none():
    sig = compute_signal(_candles([100.0] * 10), fast=9, slow=39, slope_lookback=5)
    assert sig.action is None
    assert "insufficient" in sig.reason


def test_no_crossover_when_trend_persistent():
    # Monotonic uptrend: after warm-up the fast EMA stays above the slow EMA,
    # so there is never a fresh cross on the last two bars.
    closes = [100.0 + i for i in range(80)]
    sig = compute_signal(_candles(closes), fast=9, slow=39, slope_lookback=5)
    assert sig.action is None
    assert sig.cross_up is False and sig.cross_dn is False


# ── compute_signal: bullish ─────────────────────────────────────────────────────

def test_bullish_crossover_with_slope_fires_buy():
    # Long decline, then a sharp rally that pulls the fast EMA up through the slow.
    closes = [200.0 - i * 0.5 for i in range(60)] + [170.0 + i * 3.0 for i in range(1, 40)]
    idx = _first_cross_up_index(closes, 9, 39)
    assert idx is not None
    sig = compute_signal(_candles(closes[: idx + 1]), fast=9, slow=39, slope_lookback=5, slope_min_pct=0.0)
    assert sig.action == "BUY"
    assert sig.cross_up is True
    assert sig.slope_pct is not None and sig.slope_pct > 0


def test_bullish_crossover_blocked_by_flat_slope():
    closes = [200.0 - i * 0.5 for i in range(60)] + [170.0 + i * 3.0 for i in range(1, 40)]
    idx = _first_cross_up_index(closes, 9, 39)
    assert idx is not None
    # An unreachably high slope threshold turns the trend filter into a veto.
    sig = compute_signal(_candles(closes[: idx + 1]), fast=9, slow=39, slope_lookback=5, slope_min_pct=5.0)
    assert sig.action is None
    assert sig.cross_up is True          # the cross happened...
    assert "trend filter" in sig.reason  # ...but the slope gate rejected it


# ── compute_signal: bearish ─────────────────────────────────────────────────────

def test_bearish_crossover_with_slope_fires_sell():
    closes = [100.0 + i * 0.5 for i in range(60)] + [130.0 - i * 3.0 for i in range(1, 40)]
    idx = _first_cross_dn_index(closes, 9, 39)
    assert idx is not None
    sig = compute_signal(_candles(closes[: idx + 1]), fast=9, slow=39, slope_lookback=5, slope_min_pct=0.0)
    assert sig.action == "SELL"
    assert sig.cross_dn is True
    assert sig.slope_pct is not None and sig.slope_pct < 0


def test_fast_must_be_below_slow():
    sig = compute_signal(_candles([100.0] * 100), fast=39, slow=9, slope_lookback=5)
    assert sig.action is None
    assert "must be <" in sig.reason


def test_signal_is_frozen_dataclass():
    sig = AnalyzerSignal("BUY", "x")
    try:
        sig.action = "SELL"  # type: ignore[misc]
    except Exception as exc:
        assert "cannot assign" in str(exc).lower() or "frozen" in str(exc).lower()
    else:
        raise AssertionError("AnalyzerSignal should be immutable")
