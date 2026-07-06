#!/usr/bin/env python3
"""
Fetch NIFTY and BANKNIFTY underlying spot/futures daily candle data
from Upstox for the 30 trading days before each monthly expiry.

Uses NSE_INDEX historical candle endpoint (daily interval) which is
available via the standard Upstox v2 API (no Plus subscription needed
for daily data).

Data saved to: data/{underlying}_futures/{YYYY-MM}/daily_candles.csv

Usage:
    python scripts/fetch_upstox_futures.py
    python scripts/fetch_upstox_futures.py --underlying NIFTY
    python scripts/fetch_upstox_futures.py --years 2
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE_URL   = "https://api.upstox.com/v2"
TOKEN_FILE = Path(".upstox_token")
FETCH_DELAY = 0.5  # daily candles - lighter endpoint, faster rate OK

UNDERLYING_KEYS = {
    "NIFTY":     "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}

OUT_DIRS = {
    "NIFTY":     "data/nifty_futures",
    "BANKNIFTY": "data/banknifty_futures",
}

# Monthly expiries for NIFTY (last Thursday) and BANKNIFTY (historically varied)
# We derive these from our existing backtest CSVs
MONTHLY_EXPIRY_CSV = {
    "NIFTY":     "data/analysis/nifty_monthly_trades.csv",
    "BANKNIFTY": "data/analysis/banknifty_monthly_trades.csv",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

FIELDS = ["date", "open", "high", "low", "close", "volume", "oi",
          "expiry", "days_to_expiry", "underlying"]


def _load_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    print("No access token. Run:  python scripts/fetch_upstox_options.py --login")
    sys.exit(1)


def _api_get(endpoint: str, token: str, params: dict | None = None,
             _retries: int = 5) -> dict:
    url = f"{BASE_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json", "User-Agent": _UA},
    )
    for attempt in range(_retries):
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                wait = 2 ** (attempt + 2)
                print(f"      429 rate limit - waiting {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}") from e
    raise RuntimeError(f"Exceeded {_retries} retries")


def get_monthly_expiries(underlying: str, min_date: date) -> list[date]:
    """Load monthly expiry dates from existing backtest CSV."""
    csv_path = Path(MONTHLY_EXPIRY_CSV[underlying])
    if not csv_path.exists():
        print(f"  Warning: {csv_path} not found, cannot determine expiries")
        return []
    expiries = set()
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            exp = date.fromisoformat(row["expiry_date"][:10])
            if exp >= min_date:
                expiries.add(exp)
    return sorted(expiries)


def fetch_candles(underlying: str, instrument_key: str,
                  from_dt: date, to_dt: date, token: str) -> list[dict]:
    """Fetch daily candles for instrument_key between from_dt and to_dt."""
    encoded_key = urllib.parse.quote(instrument_key, safe="")
    endpoint = (f"/historical-candle/{encoded_key}/day/"
                f"{to_dt.isoformat()}/{from_dt.isoformat()}")
    try:
        r = _api_get(endpoint, token)
        raw = r.get("data", {}).get("candles", [])
        rows = []
        for c in raw:
            # c = [datetime_str, open, high, low, close, volume, oi]
            dt_str = c[0][:10]  # YYYY-MM-DD
            rows.append({
                "date":    dt_str,
                "open":    c[1],
                "high":    c[2],
                "low":     c[3],
                "close":   c[4],
                "volume":  c[5],
                "oi":      c[6],
            })
        return rows
    except RuntimeError as e:
        print(f"      fetch failed: {e}")
        return []


def fetch_expiry_window(underlying: str, expiry: date,
                        instrument_key: str, out_dir: Path,
                        token: str, trading_days_back: int = 30) -> int:
    """Fetch ~trading_days_back of daily candles before expiry, save to CSV."""
    month_str = expiry.strftime("%Y-%m")
    out_file = out_dir / month_str / f"{expiry.isoformat()}_daily.csv"

    if out_file.exists() and out_file.stat().st_size > 100:
        print(f"  {expiry}: already done, skipping")
        return 0

    out_file.parent.mkdir(parents=True, exist_ok=True)

    # Fetch ~45 calendar days before expiry to get ~30 trading days
    from_dt = expiry - timedelta(days=45)
    candles = fetch_candles(underlying, instrument_key, from_dt, expiry, token)

    if not candles:
        print(f"  {expiry}: no data")
        return 0

    # Keep only last trading_days_back entries (sorted by date, most recent last)
    candles_sorted = sorted(candles, key=lambda x: x["date"])
    candles_trimmed = candles_sorted[-trading_days_back:]

    # Add metadata columns
    expiry_date_str = expiry.isoformat()
    for c in candles_trimmed:
        c["expiry"] = expiry_date_str
        c["days_to_expiry"] = (expiry - date.fromisoformat(c["date"])).days
        c["underlying"] = underlying

    with open(out_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(candles_trimmed)

    print(f"  {expiry}: {len(candles_trimmed)} trading days -> {out_file.name}")
    return len(candles_trimmed)


def combine_to_master(underlying: str, out_dir: Path) -> None:
    """Merge all per-expiry CSVs into a single master file."""
    master = out_dir / f"{underlying.lower()}_spot_daily.csv"
    all_rows = []
    for f in sorted(out_dir.rglob("*_daily.csv")):
        if f == master:  # skip the master file itself
            continue
        with open(f) as fh:
            rows = list(csv.DictReader(fh))
            all_rows.extend(rows)

    if not all_rows:
        return
    with open(master, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(all_rows, key=lambda x: (x["expiry"], x["date"])))
    print(f"\n  Saved master: {master} ({len(all_rows)} rows)")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch NIFTY/BANKNIFTY daily spot candles around monthly expiries"
    )
    parser.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "ALL"],
                        default="ALL")
    parser.add_argument("--years", type=int, default=2,
                        help="Years of history to fetch (default: 2)")
    parser.add_argument("--days", type=int, default=30,
                        help="Trading days before expiry to fetch (default: 30)")
    args = parser.parse_args()

    token = _load_token()
    underlyings = ["NIFTY", "BANKNIFTY"] if args.underlying == "ALL" else [args.underlying]
    min_date = date.today().replace(year=date.today().year - args.years)

    total_rows = 0
    for und in underlyings:
        inst_key = UNDERLYING_KEYS[und]
        out_dir  = Path(OUT_DIRS[und])
        out_dir.mkdir(parents=True, exist_ok=True)

        expiries = get_monthly_expiries(und, min_date)
        if not expiries:
            print(f"\n{und}: no expiries found (run backtest first to generate CSV)")
            continue

        print(f"\n{'='*60}")
        print(f"  {und} - {len(expiries)} monthly expiries from {expiries[0]} to {expiries[-1]}")
        print(f"  Fetching {args.days} trading days before each expiry...")
        print(f"{'='*60}")

        for expiry in expiries:
            rows = fetch_expiry_window(und, expiry, inst_key, out_dir, token,
                                       trading_days_back=args.days)
            total_rows += rows
            time.sleep(FETCH_DELAY)

        combine_to_master(und, out_dir)

    print(f"\nTotal rows fetched: {total_rows}")
    print("Done.")


if __name__ == "__main__":
    main()
