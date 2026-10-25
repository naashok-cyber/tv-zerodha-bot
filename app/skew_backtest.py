"""Backtest: which direction signal should skew the band hedge's landing?

The band hedge lands δ a little past zero after each rebalance (≤ the skew cap,
`NG_HEDGE_ST_SKEW_MAX_UNITS`). Without a skew that side is a coin flip; with
one it follows a direction signal. So the skew's *incremental* P&L over the
legacy solve is, per rebalance,

    skew_units × dir × ΔF      (₹, since NG F is ₹/mmBtu)

held until the next rebalance. This replays that on futures candles for each
candidate signal, with no look-ahead (every signal at bar t uses bars ≤ t):

    supertrend   Supertrend(10, 3) — what the bot runs today
    bb_mid       sign(close − SMA20)  (the Bollinger midline)
    agree        Supertrend when bb_mid agrees, else no skew (land nearest zero)
    st_bbw       Supertrend only while the BB(20, 2) width is above its rolling
                 median (no squeeze), else no skew
    always_long  +1 every bar — the period's drift; any signal must beat this
                 *and* its mirror, or it is only riding the tape

What it does NOT model: option δ/γ/θ, the real breach timing, or fill costs.
The skew only chooses between the two lot counts either side of zero, so it
adds about one lot's spread on some hedges and nothing else — small next to
the numbers here, but not zero.

Usage (inside the bot container — Kite mode needs a fresh session):

    python -m app.skew_backtest --days 90
    python -m app.skew_backtest --csv data/ng_options/NG_futures_5min.csv
    python -m app.skew_backtest --csv tv_export.csv          # TradingView export

CSV mode accepts `date,time,open,high,low,close[,tradingsymbol]` (the format of
`data/ng_options/NG_futures_5min.csv`) or a TradingView "Export chart data" file
(`time` as unix seconds or ISO). Bars are resampled to 10 minutes to match the
live ADX/Supertrend candles. With several tradingsymbols, the one with the most
bars on each date is kept (the liquid front month).
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from app.adx import compute_adx
from app.config import IST
from app.supertrend import supertrend_series

_INTERVAL_MIN = 10
_ADX_WINDOW = 14 * 3 + 5       # same lookback the live hedge feeds compute_adx
SIGNALS = ("supertrend", "bb_mid", "agree", "st_bbw", "always_long")


# ── Data loading ─────────────────────────────────────────────────────────────

def _parse_dt(row: dict) -> datetime:
    if row.get("date") and row.get("time") and ":" in str(row["time"]):
        dt = datetime.fromisoformat(f"{row['date']} {row['time']}")
    else:
        raw = str(row.get("time") or row.get("datetime") or row.get("date")).strip()
        dt = (datetime.fromtimestamp(float(raw), IST) if raw.replace(".", "").isdigit()
              else datetime.fromisoformat(raw.replace("Z", "+00:00")))
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


def load_csv(path: str) -> list[dict]:
    """Bars from CSV, front contract per date when a tradingsymbol column exists."""
    with open(path, newline="") as f:
        rows = [{k.strip().lower(): v for k, v in r.items() if k} for r in csv.DictReader(f)]
    bars = []
    for r in rows:
        try:
            bars.append({
                "date": _parse_dt(r), "sym": r.get("tradingsymbol") or "",
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    if len({b["sym"] for b in bars}) > 1:
        counts: dict = defaultdict(lambda: defaultdict(int))
        for b in bars:
            counts[b["date"].date()][b["sym"]] += 1
        front = {d: max(c, key=c.get) for d, c in counts.items()}
        bars = [b for b in bars if b["sym"] == front[b["date"].date()]]
    bars.sort(key=lambda b: b["date"])
    return bars


def load_kite(days: int, underlying: str = "NATURALGAS") -> list[dict]:
    """10-min futures candles from Kite, continuous series when Kite allows it."""
    from sqlalchemy.orm import sessionmaker

    from app.commodity_agents.strikes import front_month_future
    from app.kite_session import get_session_manager
    from app.storage import init_db

    kite = get_session_manager().get_kite()
    now = datetime.now(IST)
    with sessionmaker(bind=init_db())() as session:
        fut = front_month_future(session, underlying, now.date())
    if fut is None:
        raise SystemExit(f"no front-month {underlying} future in the instruments table")

    def _fetch(continuous: bool) -> list[dict]:
        out, cursor = [], now - timedelta(days=days)
        while cursor < now:          # 10-minute requests cap at 100 days
            end = min(cursor + timedelta(days=90), now)
            out.extend(kite.historical_data(
                fut.instrument_token, cursor, end, f"{_INTERVAL_MIN}minute",
                continuous=continuous,
            ))
            cursor = end
        return out

    try:
        raw = _fetch(True)
    except Exception as exc:
        print(f"continuous fetch refused ({exc}) — front contract {fut.tradingsymbol} only")
        raw = _fetch(False)
    return [{"date": c["date"].astimezone(IST), "open": c["open"], "high": c["high"],
             "low": c["low"], "close": c["close"]} for c in raw]


def resample(bars: list[dict], minutes: int = _INTERVAL_MIN) -> list[dict]:
    """Clock-aligned OHLC buckets; a no-op on bars already at `minutes`."""
    out: list[dict] = []
    for b in bars:
        d = b["date"]
        key = d.replace(minute=d.minute - d.minute % minutes, second=0, microsecond=0)
        if out and out[-1]["date"] == key:
            o = out[-1]
            o["high"] = max(o["high"], b["high"])
            o["low"] = min(o["low"], b["low"])
            o["close"] = b["close"]
        else:
            out.append({"date": key, "open": b["open"], "high": b["high"],
                        "low": b["low"], "close": b["close"]})
    return out


# ── Signals ──────────────────────────────────────────────────────────────────

def _regime(adx: float | None) -> str:
    if adx is None:
        return "n/a"
    return "CHOPPY" if adx < 20 else "MILD" if adx < 25 else "TRENDING"


def signals(bars: list[dict], st_period: int = 10, st_mult: float = 3.0,
            bb_period: int = 20, bb_std: float = 2.0) -> list[dict]:
    """Per-bar signal values using bars[0..i] only."""
    st = supertrend_series(bars, st_period, st_mult)
    closes = [b["close"] for b in bars]
    widths: list[float] = []
    rows = []
    for i, b in enumerate(bars):
        row: dict[str, Any] = {"i": i, "st": st[i], "bb": None, "wide": None,
                               "regime": _regime(compute_adx(bars[max(0, i - _ADX_WINDOW + 1): i + 1]))}
        if i + 1 >= bb_period:
            win = closes[i + 1 - bb_period: i + 1]
            mid = sum(win) / bb_period
            sd = statistics.pstdev(win)           # TradingView uses population σ
            row["bb"] = 1 if b["close"] > mid else -1 if b["close"] < mid else 0
            width = (2 * bb_std * sd) / mid if mid else 0.0
            widths.append(width)
            recent = widths[-100:]                # rolling median over ~2 sessions
            if len(recent) >= 20:
                row["wide"] = width > statistics.median(recent)
        rows.append(row)
    return rows


def _dir(sig: str, row: dict) -> int | None:
    st, bb = row["st"], row["bb"]
    if st is None or bb is None:
        return None                      # still warming up — not counted anywhere
    if sig == "supertrend":
        return st
    if sig == "bb_mid":
        return bb
    if sig == "agree":
        return st if st == bb else 0
    if sig == "st_bbw":
        return None if row["wide"] is None else (st if row["wide"] else 0)
    if sig == "always_long":
        return 1
    raise ValueError(sig)


# ── Replay ───────────────────────────────────────────────────────────────────

def replay(bars: list[dict], horizon: int, skew_units: float,
           sig_rows: list[dict] | None = None) -> dict[str, dict]:
    """Hold dir × skew_units from bar t's close to bar t+horizon's close.

    Never across a session boundary: the hedge freezes before squareoff, so an
    overnight gap is not something the skew is ever exposed to.
    """
    sig_rows = sig_rows or signals(bars)
    res = {s: {"n": 0, "active": 0, "hits": 0, "pnl": 0.0, "flips": 0, "indep": [],
               "by_regime": defaultdict(lambda: [0, 0.0])} for s in SIGNALS}
    last_side: dict[str, int] = {}
    for t in range(len(bars) - horizon):
        if bars[t + horizon]["date"].date() != bars[t]["date"].date():
            continue
        move = bars[t + horizon]["close"] - bars[t]["close"]
        row = sig_rows[t]
        for s in SIGNALS:
            d = _dir(s, row)
            if d is None:
                continue
            r = res[s]
            r["n"] += 1
            if d == 0:
                continue
            pnl = d * skew_units * move
            r["active"] += 1
            r["hits"] += 1 if d * move > 0 else 0
            r["pnl"] += pnl
            if t % horizon == 0:          # non-overlapping subsample for the t-stat
                r["indep"].append(pnl)
            reg = r["by_regime"][row["regime"]]
            reg[0] += 1
            reg[1] += pnl
            if last_side.get(s) not in (None, d):
                r["flips"] += 1
            last_side[s] = d
    return res


def t_stat(samples: list[float]) -> float:
    """Mean / standard error on non-overlapping holds; |t| < 2 is noise."""
    if len(samples) < 3:
        return 0.0
    sd = statistics.stdev(samples)
    return statistics.mean(samples) / (sd / math.sqrt(len(samples))) if sd else 0.0


def report(bars: list[dict], horizons: tuple[int, ...], skew_units: float) -> str:
    sig_rows = signals(bars)
    days = len({b["date"].date() for b in bars})
    first, last = bars[0], bars[-1]
    lines = [
        f"NG skew backtest — {len(bars)} x {_INTERVAL_MIN}-min bars, {days} sessions, "
        f"{first['date']:%Y-%m-%d} → {last['date']:%Y-%m-%d}",
        f"F {first['close']:.1f} → {last['close']:.1f} "
        f"({(last['close'] / first['close'] - 1) * 100:+.1f}% drift over the sample)",
        f"P&L = skew {skew_units:.0f} mmBtu x dir x ΔF, per evaluation bar (₹). "
        "'hit' = signal side matched the move.",
    ]
    for h in horizons:
        res = replay(bars, h, skew_units, sig_rows)
        lines.append("")
        lines.append(f"── hold {h} bar(s) = {h * _INTERVAL_MIN} min ──")
        lines.append(f"{'signal':<12} {'active':>7} {'skew%':>6} {'hit%':>6} "
                     f"{'₹ total':>11} {'₹/active':>9} {'t':>5} {'flips/day':>9}   "
                     f"₹ by ADX regime (CHOPPY / MILD / TRENDING)")
        for s in SIGNALS:
            r = res[s]
            act = r["active"]
            reg = r["by_regime"]
            reg_s = " / ".join(f"{reg[k][1]:+,.0f}" for k in ("CHOPPY", "MILD", "TRENDING"))
            lines.append(
                f"{s:<12} {act:>7} {act / r['n'] * 100 if r['n'] else 0:>5.0f}% "
                f"{r['hits'] / act * 100 if act else 0:>5.1f}% {r['pnl']:>+11,.0f} "
                f"{r['pnl'] / act if act else 0:>+9.1f} {t_stat(r['indep']):>+5.1f} {r['flips'] / max(days, 1):>9.1f}   {reg_s}"
            )
    lines.append("")
    lines.append("Read: a signal earns its place only if it beats always_long, its "
                 "₹ total holds up in CHOPPY, and |t| >= 2 (t uses non-overlapping "
                 "holds only). Below that it is indistinguishable from luck — a pure "
                 "random walk routinely shows ₹ totals this size.")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="OHLC CSV (see module docstring for formats)")
    src.add_argument("--days", type=int, help="Kite mode: this many days of 10-min candles")
    p.add_argument("--horizons", default="1,3,6",
                   help="hold lengths in 10-min bars (default 1,3,6 = 10/30/60 min)")
    p.add_argument("--skew-units", type=float, default=200.0,
                   help="average landing size in mmBtu (default 200 ≈ mean of a 0–400 cap)")
    args = p.parse_args()

    bars = load_csv(args.csv) if args.csv else load_kite(args.days)
    bars = resample(bars)
    if len(bars) < 60:
        raise SystemExit(f"only {len(bars)} bars — need at least 60")
    print(report(bars, tuple(int(x) for x in args.horizons.split(",")), args.skew_units))


if __name__ == "__main__":
    main()
