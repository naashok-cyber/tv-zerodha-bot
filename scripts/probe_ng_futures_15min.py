#!/usr/bin/env python3
"""
Probe all listed NATURALGAS futures tokens for 15-min depth,
then fetch the maximum available 15-min data for each contract.

Run inside Docker:
    docker compose run --rm -v $(pwd)/scripts:/app/scripts bot python scripts/probe_ng_futures_15min.py
"""
import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")
from app.kite_session import get_session_manager

kite  = get_session_manager().get_kite()
OUT   = Path("data/ng_options")
OUT.mkdir(parents=True, exist_ok=True)

END   = datetime(2026, 6, 12, 23, 59, 59)

# ── 1. Discover all listed NATURALGAS futures ─────────────────────────────────
instr   = kite.instruments("MCX")
ng_futs = sorted(
    [r for r in instr if r["name"] == "NATURALGAS" and r["instrument_type"] == "FUT"],
    key=lambda x: x["expiry"],
)
time.sleep(0.4)

print("All listed NATURALGAS futures:")
for f in ng_futs:
    print(f"  {f['tradingsymbol']}  token={f['instrument_token']}  expiry={f['expiry']}")
print()

# ── 2. Probe each token: find earliest 15-min bar available ──────────────────
FIELDS = ["date", "time", "open", "high", "low", "close", "volume", "tradingsymbol"]

all_rows = []
summary  = []

for fut in ng_futs:
    token = fut["instrument_token"]
    sym   = fut["tradingsymbol"]
    exp   = fut["expiry"]

    # Try windows of 100, 150, 190 days back from today.
    # First success tells us where data starts; then fetch that full window.
    earliest_start = None
    bar_count      = 0

    for days_back in [100, 150, 190]:
        start = END.date().__class__(2026, 6, 12) - timedelta(days=days_back)
        start_dt = datetime(start.year, start.month, start.day)
        try:
            bars = kite.historical_data(
                token, start_dt, END, "15minute", continuous=False, oi=False
            ) or []
            time.sleep(0.35)
            if bars:
                earliest_start = bars[0]["date"].date()
                bar_count      = len(bars)
                print(f"  {sym}: {days_back}d window -> {bar_count} bars, earliest={earliest_start}")
                # Save to CSV
                csv_path = OUT / f"{sym}_15min.csv"
                with csv_path.open("w", newline="") as f_csv:
                    w = csv.DictWriter(f_csv, fieldnames=FIELDS)
                    w.writeheader()
                    for b in bars:
                        dt = b["date"]
                        w.writerow({
                            "date":          dt.strftime("%Y-%m-%d"),
                            "time":          dt.strftime("%H:%M:%S"),
                            "open":          b["open"],
                            "high":          b["high"],
                            "low":           b["low"],
                            "close":         b["close"],
                            "volume":        b.get("volume", ""),
                            "tradingsymbol": sym,
                        })
                print(f"    -> Saved {bar_count} rows to {csv_path.name}")
                all_rows.extend(bars)
                break
            else:
                print(f"  {sym}: {days_back}d window -> 0 bars")
        except Exception as exc:
            print(f"  {sym}: {days_back}d window -> ERROR: {exc}")
            time.sleep(0.35)

    summary.append({
        "symbol":   sym,
        "token":    token,
        "expiry":   str(exp),
        "earliest": str(earliest_start) if earliest_start else "N/A",
        "bars":     bar_count,
    })

# ── 3. Summary ────────────────────────────────────────────────────────────────
print()
print("=" * 55)
print("SUMMARY")
print("=" * 55)
for s in summary:
    print(f"  {s['symbol']:30s}  earliest={s['earliest']}  bars={s['bars']}")

print()
print(f"Total 15-min bars across all contracts: {len(all_rows)}")
print(f"Files written to: {OUT.resolve()}")
