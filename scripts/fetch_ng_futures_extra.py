#!/usr/bin/env python3
"""
Fetch daily-continuous (1 yr) and 60-min NG futures from Kite.
Run inside Docker: docker compose run --rm -v $(pwd)/scripts:/app/scripts bot python scripts/fetch_ng_futures_extra.py
"""
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")
from app.kite_session import get_session_manager

kite = get_session_manager().get_kite()

# Front-month NATURALGAS futures token (stays valid until expiry Jun 23)
TOKEN = 129091847
OUT   = Path("data/ng_options")
OUT.mkdir(parents=True, exist_ok=True)

FIELDS = ["date", "time", "open", "high", "low", "close", "volume", "interval", "continuous"]


def save(path: Path, bars: list, interval: str, cont: bool) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for b in bars:
            dt = b["date"]
            w.writerow({
                "date":       dt.strftime("%Y-%m-%d"),
                "time":       dt.strftime("%H:%M:%S"),
                "open":       b["open"],
                "high":       b["high"],
                "low":        b["low"],
                "close":      b["close"],
                "volume":     b.get("volume", ""),
                "interval":   interval,
                "continuous": str(cont),
            })


START = datetime(2025, 6, 13)
END   = datetime(2026, 6, 12, 23, 59, 59)

# ── 1. Daily continuous — full 1 year ─────────────────────────────────────────
print("Fetching daily continuous…")
bars = kite.historical_data(TOKEN, START, END, "day", continuous=True, oi=False) or []
time.sleep(0.4)
if bars:
    save(OUT / "NG_futures_daily_continuous.csv", bars, "day", True)
    print(f"  daily continuous: {len(bars)} bars  {bars[0]['date'].date()} → {bars[-1]['date'].date()}")
else:
    print("  daily continuous: 0 bars")

# ── 2. 60-min non-continuous — ~141 days (best available intraday going back) ─
print("Fetching 60-min (non-continuous)…")
bars60 = kite.historical_data(TOKEN, START, END, "60minute", continuous=False, oi=False) or []
time.sleep(0.4)
if bars60:
    save(OUT / "NG_futures_60min.csv", bars60, "60minute", False)
    print(f"  60-min: {len(bars60)} bars  {bars60[0]['date'].date()} → {bars60[-1]['date'].date()}")
else:
    print("  60-min: 0 bars")

print("Done.")
