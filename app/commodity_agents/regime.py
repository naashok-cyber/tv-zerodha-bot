"""Deterministic regime classification — no LLM involvement (safety-critical).

Labels, in priority order:
  1. high-vol-expansion   — recent realized vol or band width blowing out;
                            short-premium debate is pointless here, pipeline
                            can skip the LLM round entirely.
  2. trending             — ADX >= 25 (Wilder), directional risk elevated.
  3. pre-event-compression— inside/near a high-impact event blackout window.
  4. range-bound          — none of the above; the classic short-premium regime.
"""
from __future__ import annotations

import math
import statistics

from app.adx import compute_adx
from app.commodity_agents.models import (
    HIGH_VOL_EXPANSION,
    PRE_EVENT_COMPRESSION,
    RANGE_BOUND,
    TRENDING,
    RegimeSnapshot,
)

ADX_TREND_THRESHOLD = 25.0
# expansion = recent short-window RV exceeds prior baseline by this factor
VOL_EXPANSION_RATIO = 1.5
BB_PERIOD = 20
RV_RECENT_WINDOW = 5
RV_BASELINE_WINDOW = 20


def _log_returns(closes: list[float]) -> list[float]:
    return [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]


def realized_vol(closes: list[float], periods_per_year: float) -> float | None:
    """Annualized close-to-close volatility. None if < 3 usable returns."""
    rets = _log_returns(closes)
    if len(rets) < 3:
        return None
    return statistics.stdev(rets) * math.sqrt(periods_per_year)


def bollinger_width_pct(closes: list[float], period: int = BB_PERIOD) -> float | None:
    """(upper - lower) / middle of a 20-period, 2-sigma Bollinger band, in %."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = statistics.mean(window)
    if mid == 0:
        return None
    sd = statistics.pstdev(window)
    return (4.0 * sd / mid) * 100.0


def iv_percentile(current_iv: float, iv_history: list[float]) -> float | None:
    """Rank of current IV against stored history (0-100). None if < 10 samples.

    History self-bootstraps: each pipeline run stores its ATM IV, so the
    percentile becomes meaningful after a couple of weeks of runs.
    """
    if len(iv_history) < 10:
        return None
    below = sum(1 for v in iv_history if v < current_iv)
    return 100.0 * below / len(iv_history)


def classify_regime(
    candles: list[dict],
    periods_per_year: float,
    atm_iv: float | None,
    iv_history: list[float],
    in_event_window: bool,
    adx_period: int = 14,
) -> RegimeSnapshot:
    """Classify from OHLC candles (each dict: high/low/close) + IV context.

    `periods_per_year` scales realized vol to annual terms (252 for daily
    candles, 252*13 for 30-minute MCX-session candles, etc.).
    """
    notes: list[str] = []
    closes = [c["close"] for c in candles]

    adx = compute_adx(candles, period=adx_period)
    bb = bollinger_width_pct(closes)
    rv = realized_vol(closes, periods_per_year)

    ivp = iv_percentile(atm_iv, iv_history) if atm_iv is not None else None
    iv_rv = (atm_iv / rv) if (atm_iv and rv) else None

    # Vol expansion: recent short-window RV vs the baseline window before it.
    # A 5-sample stdev alone is too noisy (random wiggle can read 1.5x), so we
    # also require at least one recent candle move beyond 3 sigma of baseline —
    # that's what a genuine blowout looks like, and noise almost never does.
    expansion = False
    if len(closes) >= RV_RECENT_WINDOW + RV_BASELINE_WINDOW + 1:
        recent_closes = closes[-(RV_RECENT_WINDOW + 1):]
        baseline_closes = closes[-(RV_RECENT_WINDOW + RV_BASELINE_WINDOW + 1):-RV_RECENT_WINDOW]
        recent = realized_vol(recent_closes, periods_per_year)
        baseline = realized_vol(baseline_closes, periods_per_year)
        if recent and baseline and baseline > 0 and recent / baseline >= VOL_EXPANSION_RATIO:
            base_rets = _log_returns(baseline_closes)
            sigma = statistics.stdev(base_rets) if len(base_rets) >= 3 else None
            recent_rets = _log_returns(recent_closes)
            if sigma and any(abs(r) > 3.0 * sigma for r in recent_rets):
                expansion = True
                notes.append(
                    f"recent RV {recent:.1%} vs baseline {baseline:.1%} "
                    f"(x{recent / baseline:.2f}), >3-sigma candle present"
                )

    if expansion:
        label = HIGH_VOL_EXPANSION
    elif adx is not None and adx >= ADX_TREND_THRESHOLD:
        label = TRENDING
        notes.append(f"ADX {adx:.1f} >= {ADX_TREND_THRESHOLD}")
    elif in_event_window:
        label = PRE_EVENT_COMPRESSION
        notes.append("inside high-impact event blackout window")
    else:
        label = RANGE_BOUND

    return RegimeSnapshot(
        label=label,
        adx=adx,
        bb_width_pct=bb,
        realized_vol=rv,
        atm_iv=atm_iv,
        iv_rv_ratio=iv_rv,
        iv_percentile=ivp,
        notes=notes,
    )
