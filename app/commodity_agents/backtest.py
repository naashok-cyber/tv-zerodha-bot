"""Historical replay of the deterministic gates (Phase 5 validation).

What it validates: does the regime/blackout gating actually separate calm
days from dangerous ones? For each historical session-day we classify the
regime as-of the evaluation time, then measure what happened next.

Two modes:

1. Kite mode (needs a live session) — adverse-move stats on futures candles:
    python -m app.commodity_agents.backtest --commodity NATURALGAS --days 90

2. CSV mode (offline, uses the local research data with REAL straddle PnL):
    python -m app.commodity_agents.backtest --csv-dir data/banknifty_futures
    python -m app.commodity_agents.backtest --csv-dir data/nifty_futures --eval-time 09:45

   CSV mode reads the *_monthly_5min.csv files (5-min implied-forward series
   with rolling ATM CE/PE quotes), picks the front contract per day, enters a
   short ATM straddle at --eval-time and exits at --exit-time, tracking the
   FIXED entry strike via the per-strike files in the matching *_options/
   directory (falls back to rolling-ATM premium when a strike file is absent
   — flagged in the output, slightly optimistic on trending days).

A useful result looks like: range-bound days show better short-straddle PnL
and smaller adverse moves than trending/high-vol days. If they don't, the
gate thresholds need tuning before trusting live use.
"""
from __future__ import annotations

import argparse
import csv
import logging
import random
import statistics
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Any

from app.commodity_agents import events, regime as regime_mod
from app.commodity_agents.models import COMMODITIES
from app.commodity_agents.orchestrator import _PERIODS_PER_YEAR
from app.config import IST

log = logging.getLogger(__name__)

_INTERVAL = "30minute"
_LOOKBACK_CANDLES = 60      # candles fed to the classifier per evaluation


def replay_day(
    candles_upto: list[dict],
    candles_after: list[dict],
    commodity: str,
    eval_dt: datetime,
    pre_hours: float,
    post_hours: float,
) -> dict | None:
    """Classify regime at eval_dt and measure the post-entry adverse move."""
    if len(candles_upto) < 30 or not candles_after:
        return None
    blackout = events.active_blackout(commodity, eval_dt,
                                      pre_hours=pre_hours, post_hours=post_hours)
    snap = regime_mod.classify_regime(
        candles=candles_upto[-_LOOKBACK_CANDLES:],
        periods_per_year=_PERIODS_PER_YEAR,
        atm_iv=None, iv_history=[],
        in_event_window=blackout is not None,
    )
    entry = candles_upto[-1]["close"]
    highs = max(c["high"] for c in candles_after)
    lows = min(c["low"] for c in candles_after)
    # worst excursion against a short straddle, as % of entry
    adverse_pct = 100.0 * max(highs - entry, entry - lows) / entry
    close_move_pct = 100.0 * abs(candles_after[-1]["close"] - entry) / entry
    return {
        "date": eval_dt.date().isoformat(),
        "regime": snap.label,
        "adx": round(snap.adx, 1) if snap.adx is not None else None,
        "blackout": blackout.name if blackout else "",
        "adverse_move_pct": round(adverse_pct, 3),
        "close_move_pct": round(close_move_pct, 3),
    }


def run_backtest(
    kite: Any,
    session: Any,
    commodity: str,
    days: int,
    eval_hhmm: tuple[int, int],
    settings: Any,
) -> list[dict]:
    from app.commodity_agents.strikes import front_month_future

    now = datetime.now(IST)
    fut = front_month_future(session, commodity, now.date())
    if fut is None:
        log.error("[backtest] no front-month future for %s", commodity)
        return []
    # Kite historical caps ~60 days per intraday request — chunk it.
    candles: list[dict] = []
    cursor = now - timedelta(days=days)
    while cursor < now:
        chunk_end = min(cursor + timedelta(days=55), now)
        candles.extend(kite.historical_data(fut.instrument_token, cursor, chunk_end, _INTERVAL))
        cursor = chunk_end
    if not candles:
        log.error("[backtest] no candles for %s", commodity)
        return []

    # bucket by session date (candle 'date' is tz-aware from Kite)
    by_day: dict = defaultdict(list)
    for c in candles:
        by_day[c["date"].date()].append(c)

    eval_t = time(*eval_hhmm)
    rows: list[dict] = []
    dates = sorted(by_day)
    for i, d in enumerate(dates):
        day_candles = by_day[d]
        upto = [c for c in day_candles if c["date"].time() <= eval_t]
        after = [c for c in day_candles if c["date"].time() > eval_t]
        # include prior days' candles for indicator lookback
        history = [c for dd in dates[max(0, i - 5):i] for c in by_day[dd]] + upto
        eval_dt = datetime.combine(d, eval_t, tzinfo=IST)
        row = replay_day(history, after, commodity, eval_dt,
                         settings.COMMODITY_BLACKOUT_PRE_HOURS,
                         settings.COMMODITY_BLACKOUT_POST_HOURS)
        if row:
            row["commodity"] = commodity
            rows.append(row)
    return rows


# ── CSV mode (offline research data) ─────────────────────────────────────────

def load_front_series(csv_dir: str) -> dict:
    """Per calendar day, the 5-min bars of the front (nearest-expiry) contract.

    Each *_monthly_5min.csv covers one expiry's whole life, so a trade date
    appears in several files; keep the rows with the smallest non-negative
    days_to_expiry. Bar price = implied_fwd (the OHLC columns are the daily
    bar repeated on every row).
    """
    import glob
    import os

    best: dict = {}          # date -> (days_to_expiry, expiry_str, underlying, [bars])
    for path in sorted(glob.glob(os.path.join(csv_dir, "*_monthly_5min.csv"))):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    d = datetime.strptime(row["date"], "%Y-%m-%d").date()
                    dte = int(row["days_to_expiry"])
                    bar = {
                        "time": row["time"],
                        "fwd": float(row["implied_fwd"]),
                        "atm_strike": float(row["atm_strike"]),
                        "ce": float(row["ce_price"]),
                        "pe": float(row["pe_price"]),
                    }
                except (KeyError, ValueError):
                    continue
                if dte < 0:
                    continue
                cur = best.get(d)
                if cur is None or dte < cur[0]:
                    best[d] = (dte, row["expiry"], row["underlying"], [bar])
                elif dte == cur[0]:
                    cur[3].append(bar)
    return {
        d: {"days_to_expiry": v[0], "expiry": v[1], "underlying": v[2],
            "bars": sorted(v[3], key=lambda b: b["time"])}
        for d, v in best.items()
    }


class _StrikeFileCache:
    """Reads per-strike option CSVs (e.g. 'BANKNIFTY 49000 CE 30 OCT 24_5min.csv')."""

    def __init__(self, options_dir: str) -> None:
        self._dir = options_dir
        self._cache: dict = {}

    def _path(self, underlying: str, strike: float, opt: str, expiry: str) -> str:
        import os
        exp = datetime.strptime(expiry, "%Y-%m-%d").date()
        fname = f"{underlying} {int(strike)} {opt} {exp:%d %b %y}_5min.csv".upper()
        # month folder = expiry month
        return os.path.join(self._dir, f"{exp.year}-{exp.month:02d}", fname)

    def price_at(self, underlying: str, strike: float, opt: str, expiry: str,
                 trade_date, upto_time: str) -> float | None:
        """Close of the last bar at/before upto_time on trade_date, or None."""
        import os
        path = self._path(underlying, strike, opt, expiry)
        if path not in self._cache:
            data: dict = {}
            if os.path.exists(path):
                with open(path, newline="") as f:
                    for row in csv.DictReader(f):
                        data.setdefault(row["date"], []).append(
                            (row["time"][:5], float(row["close"])))
            self._cache[path] = data
        bars = self._cache[path].get(trade_date.isoformat(), [])
        eligible = [c for (t, c) in sorted(bars) if t <= upto_time]
        return eligible[-1] if eligible else None


def run_csv_backtest(
    csv_dir: str,
    settings: Any,
    eval_time: str = "09:45",
    exit_time: str = "15:15",
    cost_pct: float = 0.0,
    slippage_pct: float = 0.0,
) -> list[dict]:
    """cost_pct: round-trip transaction costs as % of entry premium (brokerage,
    STT, exchange fees). slippage_pct: per-side fill slippage as % of premium —
    a short seller sells the entry a touch lower and buys the exit a touch
    higher. Both default to 0 (gross), matching the original behaviour."""
    series = load_front_series(csv_dir)
    if not series:
        log.error("[backtest] no *_monthly_5min.csv rows in %s", csv_dir)
        return []
    options_dir = csv_dir.replace("_futures", "_options")
    strike_files = _StrikeFileCache(options_dir)
    periods_per_year = 75 * 252     # 5-min bars, ~75 per NSE session

    rows: list[dict] = []
    dates = sorted(series)
    for i, d in enumerate(dates):
        day = series[d]
        underlying = day["underlying"]
        entry_bars = [b for b in day["bars"] if b["time"] <= eval_time]
        rest_bars = [b for b in day["bars"] if eval_time < b["time"] <= exit_time]
        if len(entry_bars) < 3 or not rest_bars:
            continue
        entry = entry_bars[-1]

        # regime from close-only candles (per-bar high/low not in this dataset)
        history = [b for dd in dates[max(0, i - 5):i] for b in series[dd]["bars"]]
        candles = [{"high": b["fwd"], "low": b["fwd"], "close": b["fwd"]}
                   for b in history + entry_bars]
        eval_dt = datetime.combine(d, time(*(int(x) for x in eval_time.split(":"))), tzinfo=IST)
        blackout = events.active_blackout(
            underlying, eval_dt,
            pre_hours=settings.COMMODITY_BLACKOUT_PRE_HOURS,
            post_hours=settings.COMMODITY_BLACKOUT_POST_HOURS)
        snap = regime_mod.classify_regime(
            candles=candles[-_LOOKBACK_CANDLES * 2:],
            periods_per_year=periods_per_year,
            atm_iv=None, iv_history=[], in_event_window=blackout is not None)

        # short ATM straddle: entry premium at the fixed entry strike
        k = entry["atm_strike"]
        entry_prem = entry["ce"] + entry["pe"]
        ce_exit = strike_files.price_at(underlying, k, "CE", day["expiry"], d, exit_time)
        pe_exit = strike_files.price_at(underlying, k, "PE", day["expiry"], d, exit_time)
        if ce_exit is not None and pe_exit is not None:
            exit_prem, exit_method = ce_exit + pe_exit, "fixed_strike"
        else:
            last = rest_bars[-1]
            exit_prem, exit_method = last["ce"] + last["pe"], "rolling_atm_approx"

        fwd_moves = [abs(b["fwd"] - entry["fwd"]) for b in rest_bars]
        rows.append({
            "commodity": underlying,
            "date": d.isoformat(),
            "regime": snap.label,
            "adx": round(snap.adx, 1) if snap.adx is not None else None,
            "blackout": blackout.name if blackout else "",
            "days_to_expiry": day["days_to_expiry"],
            "entry_strike": k,
            "entry_premium": round(entry_prem, 2),
            "exit_premium": round(exit_prem, 2),
            "exit_method": exit_method,
            "straddle_pnl_pct": round(100.0 * (entry_prem - exit_prem) / entry_prem, 2),
            "straddle_pnl_net_pct": round(
                100.0 * (entry_prem * (1 - slippage_pct / 100.0)
                         - exit_prem * (1 + slippage_pct / 100.0)) / entry_prem
                - cost_pct, 2),
            "adverse_move_pct": round(100.0 * max(fwd_moves) / entry["fwd"], 3),
            "close_move_pct": round(100.0 * abs(rest_bars[-1]["fwd"] - entry["fwd"]) / entry["fwd"], 3),
        })
    return rows


def summarize_pnl(rows: list[dict]) -> str:
    by_regime: dict = defaultdict(list)
    for r in rows:
        by_regime[r["regime"]].append(r)
    show_net = any(r.get("straddle_pnl_net_pct") != r["straddle_pnl_pct"] for r in rows)
    lines = [f"{'regime':<24}{'days':>5}{'win%':>7}{'mean pnl%':>11}"
             f"{'worst pnl%':>12}{'mean adv%':>11}"
             + (f"{'net pnl%':>10}" if show_net else "")]
    for label, rs in sorted(by_regime.items()):
        pnls = [r["straddle_pnl_pct"] for r in rs]
        wins = sum(1 for p in pnls if p > 0)
        adv = [r["adverse_move_pct"] for r in rs]
        line = (f"{label:<24}{len(rs):>5}{100.0 * wins / len(rs):>7.0f}"
                f"{statistics.mean(pnls):>11.2f}{min(pnls):>12.2f}"
                f"{statistics.mean(adv):>11.2f}")
        if show_net:
            nets = [r.get("straddle_pnl_net_pct", r["straddle_pnl_pct"]) for r in rs]
            line += f"{statistics.mean(nets):>10.2f}"
        lines.append(line)
    approx = sum(1 for r in rows if r["exit_method"] == "rolling_atm_approx")
    if approx:
        lines.append(f"note: {approx}/{len(rows)} days used rolling-ATM exit "
                     f"(strike file missing) — slightly optimistic on trend days")
    return "\n".join(lines)


def summarize(rows: list[dict]) -> str:
    by_regime: dict = defaultdict(list)
    for r in rows:
        by_regime[r["regime"]].append(r["adverse_move_pct"])
    lines = [f"{'regime':<24}{'days':>5}{'mean adverse%':>15}{'p90%':>8}{'max%':>8}"]
    for label, vals in sorted(by_regime.items()):
        vals_sorted = sorted(vals)
        p90 = vals_sorted[int(0.9 * (len(vals_sorted) - 1))]
        lines.append(f"{label:<24}{len(vals):>5}{statistics.mean(vals):>15.2f}"
                     f"{p90:>8.2f}{max(vals):>8.2f}")
    bo = [r["adverse_move_pct"] for r in rows if r["blackout"]]
    if bo:
        lines.append(f"{'(blackout days)':<24}{len(bo):>5}{statistics.mean(bo):>15.2f}")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commodity", default="ALL")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--eval-time", default=None,
                   help="entry time HH:MM (default 21:00 Kite mode, 09:45 CSV mode)")
    p.add_argument("--exit-time", default="15:15", help="CSV mode squareoff HH:MM")
    p.add_argument("--csv-dir", default=None,
                   help="offline mode: dir with *_monthly_5min.csv (e.g. data/nifty_futures)")
    p.add_argument("--with-llm", type=int, default=0, metavar="N",
                   help="also run the full debate on N random eligible days (costs API credits)")
    p.add_argument("--out", default=None, help="CSV output path")
    p.add_argument("--cost-pct", type=float, default=0.0,
                   help="round-trip costs as %% of entry premium (try 1.0)")
    p.add_argument("--slippage-pct", type=float, default=0.0,
                   help="per-side fill slippage as %% of premium (try 0.25)")
    args = p.parse_args()

    from app.config import get_settings

    if args.csv_dir:
        settings = get_settings()
        rows = run_csv_backtest(args.csv_dir, settings,
                                eval_time=args.eval_time or "09:45",
                                exit_time=args.exit_time,
                                cost_pct=args.cost_pct,
                                slippage_pct=args.slippage_pct)
        name = rows[0]["commodity"] if rows else "unknown"
        print(f"\n=== {name} CSV replay ({len(rows)} days, "
              f"entry {args.eval_time or '09:45'} exit {args.exit_time}) ===")
        print(summarize_pnl(rows) if rows else "no data")
        out = args.out or f"data/backtest_{name.lower()}_csv_{datetime.now(IST):%Y%m%d_%H%M}.csv"
        if rows:
            with open(out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            print(f"wrote {len(rows)} rows -> {out}")
        return
    from app.kite_session import get_session_manager
    from app.storage import init_db
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    kite = get_session_manager().get_kite()
    factory = sessionmaker(bind=init_db())
    hh, mm = (int(x) for x in args.eval_time.split(":"))
    targets = list(COMMODITIES) if args.commodity.upper() == "ALL" else [args.commodity.upper()]

    all_rows: list[dict] = []
    with factory() as session:
        for commodity in targets:
            rows = run_backtest(kite, session, commodity, args.days, (hh, mm), settings)
            all_rows.extend(rows)
            print(f"\n=== {commodity} ({len(rows)} days) ===")
            print(summarize(rows) if rows else "no data")

    out = args.out or f"data/backtest_commodity_{datetime.now(IST):%Y%m%d_%H%M}.csv"
    if all_rows:
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nwrote {len(all_rows)} rows -> {out}")

    if args.with_llm > 0:
        _llm_sample(all_rows, args.with_llm, settings)


def _llm_sample(rows: list[dict], n: int, settings: Any) -> None:
    """Judge sanity-check on a random sample of range-bound days: run the live
    pipeline path against today's chain but print, don't persist."""
    eligible = [r for r in rows if r["regime"] == "range-bound" and not r["blackout"]]
    if not eligible:
        print("no eligible range-bound days for LLM sampling")
        return
    sample = random.sample(eligible, min(n, len(eligible)))
    print(f"\nLLM sanity sample on {len(sample)} historical day(s) is intentionally "
          f"summary-only: full historical option chains aren't available from Kite, "
          f"so debate agents would see present-day strikes. Days selected:")
    for r in sample:
        print(f"  {r['commodity']} {r['date']} adverse={r['adverse_move_pct']}% adx={r['adx']}")
    print("Run `POST /commodity-agents/run` during market hours to exercise the "
          "full LLM path against live data instead.")


if __name__ == "__main__":
    main()
