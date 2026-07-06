#!/usr/bin/env python3
"""
Derive 5-min underlying price from options put-call parity.

For each (date, time) bar:
  implied_underlying = ATM_strike + (CE_price - PE_price)

This uses the options data already downloaded and is more accurate than
spot price since it reflects market-implied forward price at each moment.

Saves to: data/{underlying}_futures/{underlying}_5min_implied.csv

Usage:
    python scripts/derive_underlying_5min.py
    python scripts/derive_underlying_5min.py --underlying NIFTY
    python scripts/derive_underlying_5min.py --expiry-type monthly
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

OUT_ANALYSIS = Path("data/analysis")

OPTIONS_DIRS = {
    "NIFTY":     Path("data/nifty_options"),
    "BANKNIFTY": Path("data/banknifty_options"),
}

OUT_DIRS = {
    "NIFTY":     Path("data/nifty_futures"),
    "BANKNIFTY": Path("data/banknifty_futures"),
}

FIELDS = ["date", "time", "open", "high", "low", "close", "implied_fwd",
          "expiry", "days_to_expiry", "underlying", "expiry_type",
          "atm_strike", "ce_price", "pe_price", "n_strikes"]


MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_expiry_from_filename(stem: str) -> date | None:
    """
    Parse expiry from filename stem like 'NIFTY 24500 CE 28 AUG 25_5min'.
    Returns date or None.
    """
    name = stem.replace("_5min", "")
    parts = name.split()
    # Look for pattern: DD MON YY at end
    for i in range(len(parts) - 2):
        try:
            day = int(parts[i])
            mon = MONTH_MAP.get(parts[i + 1].upper())
            yr_str = parts[i + 2]
            if mon and 1 <= day <= 31 and len(yr_str) == 2 and yr_str.isdigit():
                year = 2000 + int(yr_str)
                return date(year, mon, day)
        except (ValueError, IndexError):
            continue
    return None


def parse_strike_type_from_filename(stem: str) -> tuple[int, str] | tuple[None, None]:
    """Returns (strike, 'CE'/'PE') or (None, None)."""
    name = stem.replace("_5min", "")
    parts = name.split()
    for i, p in enumerate(parts):
        if p in ("CE", "PE"):
            for j in range(i - 1, -1, -1):
                try:
                    return int(parts[j]), p
                except ValueError:
                    continue
    return None, None


def get_expiry_type(expiry: date, all_expiries: list[date]) -> str:
    """Classify as monthly (last per calendar month) or weekly."""
    same_month = [e for e in all_expiries if e.year == expiry.year and e.month == expiry.month]
    return "monthly" if expiry == max(same_month) else "weekly"


def discover_expiries(options_dir: Path) -> list[date]:
    """Scan all CSV filenames to find unique expiry dates."""
    expiries: set[date] = set()
    for month_dir in options_dir.iterdir():
        if not month_dir.is_dir():
            continue
        for f in month_dir.glob("*_5min.csv"):
            exp = parse_expiry_from_filename(f.stem)
            if exp:
                expiries.add(exp)
    return sorted(expiries)


def load_expiry_data(options_dir: Path, expiry: date) -> dict:
    """
    Load all CE/PE CSV files for this expiry (files are in YYYY-MM/ flat dir).
    Returns price_table: {(date_str, time_str): {strike: [ce, pe]}}
    """
    month_str = expiry.strftime("%Y-%m")
    month_dir = options_dir / month_str

    if not month_dir.exists():
        return {}

    price_table: dict = defaultdict(lambda: defaultdict(lambda: [None, None]))

    for f in month_dir.glob("*_5min.csv"):
        exp = parse_expiry_from_filename(f.stem)
        if exp != expiry:
            continue
        strike, opt_type = parse_strike_type_from_filename(f.stem)
        if strike is None:
            continue

        try:
            with open(f, newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    d = str(row.get("date", ""))[:10]
                    t = str(row.get("time", ""))[:5]  # HH:MM:SS -> HH:MM
                    try:
                        close_val = float(row.get("close", 0) or 0)
                    except (ValueError, TypeError):
                        continue
                    if close_val <= 0:
                        continue
                    idx = 0 if opt_type == "CE" else 1
                    price_table[(d, t)][strike][idx] = close_val
        except Exception:
            continue

    return dict(price_table)


def compute_implied_bars(price_table: dict, expiry: date,
                         underlying: str, expiry_type: str) -> list[dict]:
    """
    For each (date, time) bar, find ATM and compute implied underlying.
    ATM = strike with minimum |CE - PE|.
    implied = ATM_strike + (CE - PE)
    Also compute OHLC by grouping per 5-min bar.
    """
    rows = []
    expiry_str = expiry.isoformat()

    # Group by date first for OHLC calculation
    # implied values per bar
    bar_data: dict[str, list] = defaultdict(list)  # date -> list of (time, implied)

    for (date_str, time_str), strike_map in sorted(price_table.items()):
        # Find pairs with both CE and PE
        pairs = []
        for strike, (ce, pe) in strike_map.items():
            if ce is not None and pe is not None and ce > 0 and pe > 0:
                diff = abs(ce - pe)
                implied = strike + (ce - pe)
                pairs.append((diff, strike, ce, pe, implied))

        if len(pairs) < 2:
            continue

        pairs.sort()  # sort by |CE - PE| ascending
        # Use median of top 3 closest-to-ATM strikes
        top = pairs[:min(3, len(pairs))]
        implied_vals = [p[4] for p in top]
        implied_fwd = round(statistics.median(implied_vals), 2)

        # Best ATM (minimum diff)
        best = pairs[0]
        atm_strike = best[1]
        ce_price = round(best[2], 2)
        pe_price = round(best[3], 2)

        try:
            exp_date = date.fromisoformat(expiry_str)
            bar_date = date.fromisoformat(date_str)
            dte = (exp_date - bar_date).days
        except ValueError:
            continue

        bar_data[date_str].append({
            "date":         date_str,
            "time":         time_str,
            "implied_fwd":  implied_fwd,
            "expiry":       expiry_str,
            "days_to_expiry": dte,
            "underlying":   underlying,
            "expiry_type":  expiry_type,
            "atm_strike":   atm_strike,
            "ce_price":     ce_price,
            "pe_price":     pe_price,
            "n_strikes":    len(pairs),
        })

    # Compute OHLC per date from implied_fwd values
    for date_str, bars in sorted(bar_data.items()):
        bars_sorted = sorted(bars, key=lambda x: x["time"])
        implied_vals_day = [b["implied_fwd"] for b in bars_sorted]
        day_open  = implied_vals_day[0]
        day_high  = max(implied_vals_day)
        day_low   = min(implied_vals_day)
        day_close = implied_vals_day[-1]

        for bar in bars_sorted:
            bar["open"]  = day_open
            bar["high"]  = day_high
            bar["low"]   = day_low
            bar["close"] = day_close
            rows.append(bar)

    return rows


def process_underlying(underlying: str, expiry_type_filter: str | None = None) -> None:
    options_dir = OPTIONS_DIRS[underlying]
    out_dir     = OUT_DIRS[underlying]
    out_dir.mkdir(parents=True, exist_ok=True)

    if not options_dir.exists():
        print(f"  {underlying}: options directory not found")
        return

    print(f"  Scanning {underlying} options directory...")
    all_expiries = discover_expiries(options_dir)

    if not all_expiries:
        print(f"  {underlying}: no expiry data found")
        return

    all_rows: list[dict] = []

    print(f"\n{'='*65}")
    print(f"  {underlying} — Deriving 5-min implied underlying ({len(all_expiries)} expiries)")
    print(f"{'='*65}")

    for expiry in all_expiries:
        etype = get_expiry_type(expiry, all_expiries)
        if expiry_type_filter and etype != expiry_type_filter:
            continue

        out_file = out_dir / f"{expiry.isoformat()}_{etype}_5min.csv"
        if out_file.exists() and out_file.stat().st_size > 500:
            # Load existing for master merge
            with open(out_file, newline="") as f:
                all_rows.extend(list(csv.DictReader(f)))
            print(f"  {expiry} [{etype}]: already done ({out_file.stat().st_size//1024}KB)")
            continue

        price_table = load_expiry_data(options_dir, expiry)
        if not price_table:
            print(f"  {expiry} [{etype}]: no options data")
            continue

        bars = compute_implied_bars(price_table, expiry, underlying, etype)
        if not bars:
            print(f"  {expiry} [{etype}]: no bars computed")
            continue

        with open(out_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(bars)

        all_rows.extend(bars)
        n_days = len(set(b["date"] for b in bars))
        print(f"  {expiry} [{etype}]: {len(bars)} bars across {n_days} trading days")

    # Save master CSV
    if all_rows:
        master = out_dir / f"{underlying.lower()}_5min_implied.csv"
        all_rows_sorted = sorted(all_rows, key=lambda x: (x["expiry"], x["date"], x["time"]))
        with open(master, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(all_rows_sorted)
        monthly = sum(1 for r in all_rows if r.get("expiry_type") == "monthly")
        weekly  = sum(1 for r in all_rows if r.get("expiry_type") == "weekly")
        print(f"\n  Saved: {master}")
        print(f"  Total: {len(all_rows)} bars  (monthly={monthly}, weekly={weekly})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "ALL"], default="ALL")
    parser.add_argument("--expiry-type", choices=["monthly", "weekly"], default=None)
    args = parser.parse_args()

    underlyings = ["NIFTY", "BANKNIFTY"] if args.underlying == "ALL" else [args.underlying]
    for und in underlyings:
        process_underlying(und, args.expiry_type)

    print("\nDone.")


if __name__ == "__main__":
    main()
