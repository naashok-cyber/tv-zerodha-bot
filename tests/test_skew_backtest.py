"""The skew backtest harness — loading, resampling, no look-ahead, scoring."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from app import skew_backtest as sb
from app.config import IST
from app.supertrend import compute_supertrend, supertrend_series


def _bars(closes: list[float], start: datetime | None = None, step_min: int = 10) -> list[dict]:
    t0 = start or datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    return [{"date": t0 + timedelta(minutes=step_min * i), "open": c,
             "high": c + 0.5, "low": c - 0.5, "close": c} for i, c in enumerate(closes)]


def test_series_matches_prefix_recompute():
    """out[i] must use bars[0..i] only — the backtest's no-look-ahead guarantee."""
    closes = [300 + 8 * math.sin(i / 7) + i * 0.05 for i in range(120)]
    bars = _bars(closes)
    series = supertrend_series(bars)
    for i in range(12, 120, 7):
        assert series[i] == compute_supertrend(bars[: i + 1]), i


def test_trend_signal_earns_on_a_trend():
    bars = _bars([300 + 0.4 * i for i in range(80)])   # one session, steady rise
    res = sb.replay(bars, horizon=3, skew_units=200)
    assert res["supertrend"]["pnl"] > 0
    assert res["supertrend"]["hits"] == res["supertrend"]["active"]


def test_holds_never_cross_the_session_boundary():
    day1 = _bars([300.0] * 40)
    gap = _bars([400.0] * 40, start=datetime(2026, 9, 2, 9, 0, tzinfo=IST))
    res = sb.replay(day1 + gap, horizon=3, skew_units=200)
    assert res["always_long"]["pnl"] == 0, "the overnight +100 gap must not be scored"


def test_agree_is_flat_when_signals_disagree():
    row = {"st": 1, "bb": -1, "wide": True, "regime": "CHOPPY"}
    assert sb._dir("agree", row) == 0
    assert sb._dir("supertrend", row) == 1
    assert sb._dir("st_bbw", {**row, "wide": False}) == 0


def test_resample_five_to_ten_minutes():
    five = _bars([1, 2, 3, 4], step_min=5)
    ten = sb.resample(five)
    assert len(ten) == 2
    assert (ten[0]["open"], ten[0]["close"]) == (1, 2)
    assert ten[1]["high"] == 4.5


def test_load_csv_repo_format_keeps_front_contract(tmp_path):
    p = tmp_path / "ng.csv"
    rows = ["date,time,open,high,low,close,tradingsymbol"]
    for m in range(0, 30, 5):
        rows.append(f"2026-09-01,09:{m:02d}:00,300,301,299,300.5,NATURALGAS26SEPFUT")
    rows.append("2026-09-01,09:00:00,310,311,309,310,NATURALGAS26OCTFUT")
    p.write_text("\n".join(rows))
    bars = sb.load_csv(str(p))
    assert len(bars) == 6 and all(b["close"] == 300.5 for b in bars)


def test_load_csv_tradingview_unix_time(tmp_path):
    p = tmp_path / "tv.csv"
    t = int(datetime(2026, 9, 1, 9, 0, tzinfo=IST).timestamp())
    p.write_text(f"time,open,high,low,close\n{t},300,301,299,300.5\n{t + 600},300,302,299,301\n")
    bars = sb.load_csv(str(p))
    assert bars[0]["date"] == datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    assert bars[1]["close"] == 301


def test_report_renders():
    closes = [300 + 6 * math.sin(i / 9) for i in range(200)]
    out = sb.report(_bars(closes, step_min=2), (1, 3), 200)   # 2-min steps stay in one day
    assert "supertrend" in out and "always_long" in out
