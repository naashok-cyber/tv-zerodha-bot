"""Supertrend direction from raw OHLC candle dicts.

No external dependencies — uses only the stdlib, same as `app/adx.py`, and is
fed the very same 10-minute candles so it costs no extra historical call.
Each candle dict must have keys: high, low, close  (open/volume ignored).
"""
from __future__ import annotations


def compute_supertrend(
    candles: list[dict], period: int = 10, multiplier: float = 3.0
) -> int | None:
    """Return +1 (uptrend) or -1 (downtrend) for the latest candle, or None.

    Standard Supertrend: an ATR channel around hl2 whose bands only ratchet
    toward price, never away from it. Direction flips when the close crosses the
    opposite band. ATR uses Wilder's smoothing (alpha = 1/period), seeded with
    the simple mean of the first `period` true ranges.

    Requires at least (period + 2) candles: `period` TRs to seed the ATR, then
    at least one bar to walk the bands from.
    """
    series = supertrend_series(candles, period, multiplier)
    return series[-1] if series else None


def supertrend_series(
    candles: list[dict], period: int = 10, multiplier: float = 3.0
) -> list[int | None]:
    """Direction at every candle (None until warmed up) — one O(n) pass.

    The backtest (`app/skew_backtest.py`) needs the value as-of each bar;
    re-running `compute_supertrend` on every prefix would be O(n^2).
    `out[i]` uses candles[0..i] only, so it never looks ahead.
    """
    out: list[int | None] = [None] * len(candles)
    if period < 1 or len(candles) < period + 2:
        return out

    trs: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    atr = sum(trs[:period]) / period
    ub = lb = None
    direction = 1   # TradingView seeds the trend as up

    # trs[k] belongs to candles[k + 1]; the first ATR is ready at candles[period].
    for i in range(period, len(candles)):
        if i > period:
            atr = (atr * (period - 1) + trs[i - 1]) / period
        c = candles[i]
        hl2 = (c["high"] + c["low"]) / 2.0
        basic_ub = hl2 + multiplier * atr
        basic_lb = hl2 - multiplier * atr

        if ub is None:
            ub, lb = basic_ub, basic_lb
            continue

        # Same recurrence as TradingView's built-in, so the bot and the chart
        # agree on the direction: bands only ratchet toward price while the
        # previous close stays inside them, and a flip is judged against the
        # *previous* bar's band.
        prev_close = candles[i - 1]["close"]
        prev_ub, prev_lb = ub, lb
        lb = max(basic_lb, prev_lb) if prev_close > prev_lb else basic_lb
        ub = min(basic_ub, prev_ub) if prev_close < prev_ub else basic_ub

        if direction == -1 and c["close"] > prev_ub:
            direction = 1
        elif direction == 1 and c["close"] < prev_lb:
            direction = -1
        out[i] = direction

    return out
