"""Wilder-smoothed ADX computation from raw OHLCV candle dicts.

No external dependencies — uses only the stdlib.
Each candle dict must have keys: high, low, close  (open/volume ignored).
"""
from __future__ import annotations


def compute_adx(candles: list[dict], period: int = 14) -> float | None:
    """Return the ADX value for the given candles, or None if insufficient data.

    Requires at least (2 * period + 1) candles for a stable result.
    Uses Wilder's original smoothing (alpha = 1/period).
    """
    if len(candles) < period * 2 + 1:
        return None

    tr_vals: list[float] = []
    plus_dm_vals: list[float] = []
    minus_dm_vals: list[float] = []

    for i in range(1, len(candles)):
        high      = candles[i]["high"]
        low       = candles[i]["low"]
        prev_high = candles[i - 1]["high"]
        prev_low  = candles[i - 1]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_vals.append(tr)

        up   = high - prev_high
        down = prev_low - low
        plus_dm_vals.append(up   if (up > down and up > 0)   else 0.0)
        minus_dm_vals.append(down if (down > up and down > 0) else 0.0)

    # Seed: sum of first `period` values (Wilder's initialisation)
    atr      = sum(tr_vals[:period])
    plus_sm  = sum(plus_dm_vals[:period])
    minus_sm = sum(minus_dm_vals[:period])

    def _dx(atr_v: float, p: float, m: float) -> float:
        if atr_v == 0:
            return 0.0
        di_p = 100.0 * p / atr_v
        di_m = 100.0 * m / atr_v
        denom = di_p + di_m
        return 0.0 if denom == 0 else 100.0 * abs(di_p - di_m) / denom

    dx_vals: list[float] = [_dx(atr, plus_sm, minus_sm)]

    for i in range(period, len(tr_vals)):
        atr      = atr      - atr      / period + tr_vals[i]
        plus_sm  = plus_sm  - plus_sm  / period + plus_dm_vals[i]
        minus_sm = minus_sm - minus_sm / period + minus_dm_vals[i]
        dx_vals.append(_dx(atr, plus_sm, minus_sm))

    if len(dx_vals) < period:
        return None

    # ADX = Wilder smooth of DX values
    adx = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx
