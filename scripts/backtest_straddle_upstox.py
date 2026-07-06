"""
Short straddle backtest on Upstox 5-min option data.

Usage:
    python scripts/backtest_straddle_upstox.py
    python scripts/backtest_straddle_upstox.py --underlying NIFTY
    python scripts/backtest_straddle_upstox.py --underlying BANKNIFTY
    python scripts/backtest_straddle_upstox.py --max-days 30

Outputs to data/analysis/:
    {underlying}_{weekly|monthly}_trades.csv
    {underlying}_{weekly|monthly}_summary.csv
    combined_summary.csv
"""

import argparse
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_ROOTS = {
    "NIFTY":     "data/nifty_options",
    "BANKNIFTY": "data/banknifty_options",
}
LOT_SIZES  = {"NIFTY": 75, "BANKNIFTY": 30}

# Entry times to test: 09:15 to 14:00 every 15 min
ENTRY_TIMES = []
from datetime import datetime as _dt
_t = _dt(2000, 1, 1, 9, 15)
while _t <= _dt(2000, 1, 1, 14, 0):
    ENTRY_TIMES.append(_t.strftime("%H:%M"))
    _t = _dt(2000, 1, 1, _t.hour, _t.minute) + timedelta(minutes=15)

SL_MULTIPLIER     = 1.50   # exit when combined >= 150% of entry
TARGET_MULTIPLIER = 0.30   # exit when combined <= 30% of entry (70% captured)
MAX_HOLD_TIME     = "15:25"
MAX_DAYS_BEFORE_EXPIRY = 30  # only look at last N trading days before expiry


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------

def parse_filename(fname: str):
    name = Path(fname).stem.replace("_5min", "").replace("_1min", "")
    m = re.match(
        r"^(NIFTY|BANKNIFTY|MIDCPNIFTY)\s+(\d+)\s+(CE|PE)\s+(\d{1,2})\s+(\w{3})\s+(\d{2})$",
        name.strip(), re.IGNORECASE)
    if not m:
        return None
    underlying, strike, opt_type, day, mon, yr = m.groups()
    try:
        expiry = _dt.strptime(f"{day} {mon} {yr}", "%d %b %y").date()
    except ValueError:
        return None
    return underlying.upper(), int(strike), opt_type.upper(), expiry


def is_monthly_expiry(expiry: date, all_expiries: list[date]) -> bool:
    """Last expiry in its calendar month."""
    same_month = [e for e in all_expiries if e.year == expiry.year and e.month == expiry.month]
    return expiry == max(same_month)


# ---------------------------------------------------------------------------
# Fast price lookup builder
# ---------------------------------------------------------------------------

def build_price_table(data_dir: Path, expiry: date, underlying: str) -> dict:
    """
    Returns price_table: {(date_obj, time_str): {strike_int: [ce_close, pe_close]}}
    Built from all CSVs for this expiry. O(1) lookups in the backtest loop.
    """
    month_dir = data_dir / expiry.strftime("%Y-%m")
    if not month_dir.exists():
        return {}

    price_table: dict = defaultdict(lambda: defaultdict(lambda: [None, None]))

    for f in month_dir.glob("*.csv"):
        parsed = parse_filename(f.name)
        if parsed is None:
            continue
        und, strike, opt_type, exp = parsed
        if und != underlying or exp != expiry:
            continue

        try:
            df = pd.read_csv(f, usecols=lambda c: c in ("date", "time", "close"))
            df.columns = [c.lower() for c in df.columns]
            if "date" not in df.columns or "time" not in df.columns or "close" not in df.columns:
                continue
            df["time"] = df["time"].astype(str).str[:5]   # HH:MM:SS -> HH:MM
            df["date"] = pd.to_datetime(df["date"]).dt.date
            idx = 0 if opt_type == "CE" else 1
            for date_val, time_val, close_val in zip(df["date"], df["time"], df["close"]):
                try:
                    price_table[(date_val, time_val)][strike][idx] = float(close_val)
                except (ValueError, TypeError):
                    pass
        except Exception:
            continue

    return dict(price_table)


# ---------------------------------------------------------------------------
# Backtest for one expiry
# ---------------------------------------------------------------------------

def backtest_expiry(price_table: dict, expiry: date, underlying: str,
                    lot_size: int, expiry_type: str,
                    max_days: int = MAX_DAYS_BEFORE_EXPIRY) -> list[dict]:
    """
    For each (trade_date, entry_time): find ATM, simulate short straddle.
    Only tests the last `max_days` trading days before expiry.
    """
    if not price_table:
        return []

    # All available trading dates for this expiry
    all_dates = sorted({k[0] for k in price_table})
    # Limit to last N days before expiry
    if max_days:
        cutoff = expiry - timedelta(days=max_days * 2)  # calendar days buffer
        all_dates = [d for d in all_dates if d >= cutoff]
        all_dates = all_dates[-max_days:]  # keep at most max_days

    trades = []

    for trade_date in all_dates:
        day_name = trade_date.strftime("%A")

        for entry_time in ENTRY_TIMES:
            slot = price_table.get((trade_date, entry_time))
            if not slot:
                continue

            # Find ATM: strike where |CE - PE| is minimised, both prices > 0
            best_strike = None
            best_diff   = float("inf")
            best_ce = best_pe = 0.0

            for strike, (ce_p, pe_p) in slot.items():
                if ce_p is None or pe_p is None or ce_p <= 0 or pe_p <= 0:
                    continue
                diff = abs(ce_p - pe_p)
                if diff < best_diff:
                    best_diff   = diff
                    best_strike = strike
                    best_ce, best_pe = ce_p, pe_p

            if best_strike is None:
                continue

            entry_premium = best_ce + best_pe
            sl_level      = entry_premium * SL_MULTIPLIER
            target_level  = entry_premium * TARGET_MULTIPLIER

            # Simulate forward from entry_time
            exit_time   = None
            exit_ce     = exit_pe = 0.0
            exit_reason = "EOD"

            # Get all time slots on trade_date at or after entry_time, sorted
            day_times = sorted(
                t for (d, t) in price_table if d == trade_date and t >= entry_time
            )

            for t in day_times:
                if t > MAX_HOLD_TIME:
                    break
                bar = price_table.get((trade_date, t), {}).get(best_strike)
                if bar is None:
                    continue
                ce_v, pe_v = bar[0], bar[1]
                if ce_v is None or pe_v is None:
                    continue
                combined = ce_v + pe_v

                if t == MAX_HOLD_TIME or t == day_times[-1]:
                    exit_time, exit_ce, exit_pe, exit_reason = t, ce_v, pe_v, "EOD"
                    break
                if combined >= sl_level:
                    exit_time, exit_ce, exit_pe, exit_reason = t, ce_v, pe_v, "SL"
                    break
                if combined <= target_level:
                    exit_time, exit_ce, exit_pe, exit_reason = t, ce_v, pe_v, "TARGET"
                    break

            if exit_time is None:
                continue

            exit_premium  = exit_ce + exit_pe
            pnl_per_unit  = entry_premium - exit_premium
            pnl_pct       = (pnl_per_unit / entry_premium * 100) if entry_premium > 0 else 0.0

            trades.append({
                "underlying":    underlying,
                "expiry_date":   expiry.isoformat(),
                "expiry_type":   expiry_type,
                "trade_date":    trade_date.isoformat(),
                "day_of_week":   day_name,
                "entry_time":    entry_time,
                "exit_time":     exit_time,
                "atm_strike":    best_strike,
                "ce_entry":      round(best_ce, 2),
                "pe_entry":      round(best_pe, 2),
                "total_premium": round(entry_premium, 2),
                "ce_exit":       round(exit_ce, 2),
                "pe_exit":       round(exit_pe, 2),
                "exit_premium":  round(exit_premium, 2),
                "exit_reason":   exit_reason,
                "pnl_per_unit":  round(pnl_per_unit, 2),
                "pnl_pct":       round(pnl_pct, 2),
                "lot_size":      lot_size,
                "pnl_per_lot":   round(pnl_per_unit * lot_size, 2),
            })

    return trades


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_backtest(underlying: str, out_dir: Path, max_days: int):
    data_dir = Path(DATA_ROOTS[underlying])
    lot_size = LOT_SIZES[underlying]

    if not data_dir.exists():
        print(f"  [SKIP] {underlying}: data dir not found")
        return [], []

    # Discover expiry dates
    expiries = set()
    for month_dir in data_dir.iterdir():
        if not month_dir.is_dir():
            continue
        for f in month_dir.glob("*.csv"):
            parsed = parse_filename(f.name)
            if parsed and parsed[0] == underlying:
                expiries.add(parsed[3])
    expiries = sorted(expiries)

    print(f"\n  {underlying}: {len(expiries)} expiries found")
    if not expiries:
        return [], []

    weekly_trades, monthly_trades = [], []

    for i, expiry in enumerate(expiries):
        is_monthly = is_monthly_expiry(expiry, expiries)
        expiry_type = "monthly" if is_monthly else "weekly"
        print(f"  [{i+1}/{len(expiries)}] {expiry} ({expiry_type})", end="", flush=True)

        price_table = build_price_table(data_dir, expiry, underlying)
        if not price_table:
            print(" - no data")
            continue

        trades = backtest_expiry(price_table, expiry, underlying, lot_size, expiry_type, max_days)
        print(f" - {len(trades)} trades")

        if is_monthly:
            monthly_trades.extend(trades)
        else:
            weekly_trades.extend(trades)

    return weekly_trades, monthly_trades


# ---------------------------------------------------------------------------
# Summary + output
# ---------------------------------------------------------------------------

def build_summary(df: pd.DataFrame, underlying: str, label: str) -> pd.DataFrame:
    rows = []
    for (dow, etime), g in df.groupby(["day_of_week", "entry_time"]):
        n = len(g)
        wins = (g["pnl_pct"] > 0).sum()
        rows.append({
            "underlying":        underlying,
            "expiry_type":       label,
            "day_of_week":       dow,
            "entry_time":        etime,
            "num_trades":        n,
            "win_rate_pct":      round(wins / n * 100, 1),
            "avg_pnl_pct":       round(g["pnl_pct"].mean(), 2),
            "median_pnl_pct":    round(g["pnl_pct"].median(), 2),
            "avg_pnl_per_lot":   round(g["pnl_per_lot"].mean(), 2),
            "total_pnl_per_lot": round(g["pnl_per_lot"].sum(), 2),
            "sl_rate_pct":       round((g["exit_reason"] == "SL").sum() / n * 100, 1),
            "target_rate_pct":   round((g["exit_reason"] == "TARGET").sum() / n * 100, 1),
            "eod_rate_pct":      round((g["exit_reason"] == "EOD").sum() / n * 100, 1),
            "best_pnl_pct":      round(g["pnl_pct"].max(), 2),
            "worst_pnl_pct":     round(g["pnl_pct"].min(), 2),
            "avg_total_premium": round(g["total_premium"].mean(), 2),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["win_rate_pct", "avg_pnl_pct"], ascending=False).reset_index(drop=True)


def print_top_slots(out_dir: Path, n: int = 15):
    f = out_dir / "combined_summary.csv"
    if not f.exists():
        return
    df = pd.read_csv(f)
    df = df[(df["num_trades"] >= 3) & (df["avg_pnl_pct"] > 0)]
    top = df.nlargest(n, "win_rate_pct")
    if top.empty:
        print("\n  No profitable slots found.")
        return
    print(f"\n{'='*72}")
    print(f"  TOP {n} SHORT STRADDLE SLOTS  (min 3 trades, avg PnL > 0)")
    print(f"{'='*72}")
    print(f"{'Underlying':<12} {'Type':<8} {'Day':<10} {'Time':<6} {'N':<5} {'Win%':<6} {'AvgPnL%':<9} {'AvgPnL/lot'}")
    print("-"*72)
    for _, r in top.iterrows():
        print(f"{r['underlying']:<12} {r['expiry_type']:<8} {r['day_of_week']:<10} {r['entry_time']:<6} "
              f"{int(r['num_trades']):<5} {r['win_rate_pct']:<6} {r['avg_pnl_pct']:<9} {r['avg_pnl_per_lot']:.0f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "ALL"], default="ALL")
    parser.add_argument("--out", default="data/analysis")
    parser.add_argument("--max-days", type=int, default=MAX_DAYS_BEFORE_EXPIRY,
                        help="Max trading days before expiry to backtest (default 30)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    underlyings = ["NIFTY", "BANKNIFTY"] if args.underlying == "ALL" else [args.underlying]

    print(f"\nShort Straddle Backtest")
    print(f"  SL:       combined >= {int(SL_MULTIPLIER*100)}% of entry premium")
    print(f"  Target:   combined <= {int(TARGET_MULTIPLIER*100)}% of entry premium")
    print(f"  EOD:      {MAX_HOLD_TIME}")
    print(f"  Max days: {args.max_days} trading days before expiry")
    print(f"  Output:   {out_dir}")

    all_weekly, all_monthly = [], []

    for und in underlyings:
        w, m = run_backtest(und, out_dir, args.max_days)
        all_weekly.extend(w)
        all_monthly.extend(m)

        # Save per-underlying
        tag = und.lower()
        for label, trades in [("weekly", w), ("monthly", m)]:
            if not trades:
                continue
            df = pd.DataFrame(trades)
            df.to_csv(out_dir / f"{tag}_{label}_trades.csv", index=False)
            print(f"  Saved {tag}_{label}_trades.csv ({len(df)} rows)")
            s = build_summary(df, und, label)
            if not s.empty:
                s.to_csv(out_dir / f"{tag}_{label}_summary.csv", index=False)
                print(f"  Saved {tag}_{label}_summary.csv ({len(s)} rows)")

    # Combined summary
    dfs = []
    for f in out_dir.glob("*_summary.csv"):
        if "combined" not in f.name:
            dfs.append(pd.read_csv(f))
    if dfs:
        combined = pd.concat(dfs).sort_values(
            ["underlying", "expiry_type", "win_rate_pct", "avg_pnl_pct"],
            ascending=[True, True, False, False]).reset_index(drop=True)
        combined.to_csv(out_dir / "combined_summary.csv", index=False)
        print(f"\n  Saved combined_summary.csv ({len(combined)} rows)")

    print_top_slots(out_dir)
    print("\nDone.")


if __name__ == "__main__":
    main()
