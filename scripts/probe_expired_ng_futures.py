#!/usr/bin/env python3
"""
Try every possible method to recover instrument tokens for expired
NATURALGAS monthly futures: MAR26, APR26, MAY26.

Approach 1: kite.quote() / kite.ltp() with exchange:tradingsymbol —
  Kite sometimes returns tokens for recently expired contracts.

Approach 2: Token range scan near the known JUN26 token (129091847).
  MCX tokens are assigned sequentially at listing time; expired contracts
  may be a few million below the current front-month.

Once a token is found, try fetching 15-min / 30-min / 60-min OHLC.
"""
import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")
from app.kite_session import get_session_manager

kite = get_session_manager().get_kite()
OUT  = Path("data/ng_options")
OUT.mkdir(parents=True, exist_ok=True)

EXPIRED_SYMBOLS = [
    "NATURALGAS26MAYFUT",
    "NATURALGAS26APRFUT",
    "NATURALGAS26MARFUT",
    "NATURALGAS26FEBFUT",
    "NATURALGAS25DECFUT",
    "NATURALGAS25NOVFUT",
    "NATURALGAS25OCTFUT",
    "NATURALGAS25SEPFUT",
]

END = datetime(2026, 6, 12, 23, 59, 59)

# ── Approach 1: quote/ltp by tradingsymbol ────────────────────────────────────
print("=== Approach 1: kite.quote() for expired tradingsymbols ===")
found_tokens: dict[str, int] = {}

for sym in EXPIRED_SYMBOLS:
    key = f"MCX:{sym}"
    try:
        q = kite.quote([key])
        token = q[key]["instrument_token"]
        found_tokens[sym] = token
        ltp = q[key].get("last_price", "?")
        print(f"  {sym}: token={token}  ltp={ltp}  ← FOUND via quote()")
    except Exception as e:
        print(f"  {sym}: quote() failed — {e}")
    time.sleep(0.35)

# ── Approach 2: token range scan ─────────────────────────────────────────────
# JUN26FUT token = 129091847.  Scan 5M–40M below it in steps.
print()
print("=== Approach 2: Token range scan (15-min probe) ===")
print("  (JUN26 token = 129091847; scanning candidates below it)")

JUN26_TOKEN = 129091847
# Token candidates for up to 8 previous monthly contracts.
# MCX adds ~3M–15M per new listing; try a grid of 2M-step offsets.
SCAN_OFFSETS = [
    2_000_000, 3_000_000, 4_000_000, 5_000_000,
    6_000_000, 8_000_000, 10_000_000, 12_000_000,
    15_000_000, 20_000_000, 25_000_000, 30_000_000,
]

# Date windows: try periods when each prior month was front-month
PRIOR_WINDOWS = [
    ("MAY26", datetime(2026, 4, 27), datetime(2026, 5, 26, 23, 59, 59)),
    ("APR26", datetime(2026, 3, 30), datetime(2026, 4, 27, 23, 59, 59)),
    ("MAR26", datetime(2026, 2, 28), datetime(2026, 3, 29, 23, 59, 59)),
    ("FEB26", datetime(2026, 1, 27), datetime(2026, 2, 25, 23, 59, 59)),
]

scan_hits: list[dict] = []

for month_label, w_start, w_end in PRIOR_WINDOWS:
    print(f"\n  Scanning for {month_label}FUT  window {w_start.date()} → {w_end.date()}")
    for offset in SCAN_OFFSETS:
        candidate = JUN26_TOKEN - offset
        if candidate <= 0:
            continue
        try:
            bars = kite.historical_data(
                candidate, w_start, w_end, "60minute", continuous=False, oi=False
            ) or []
            time.sleep(0.35)
            if bars and len(bars) > 5:
                first_close = bars[0]["close"]
                last_close  = bars[-1]["close"]
                print(f"    token={candidate} (offset -{offset:,}): {len(bars)} bars  "
                      f"close {first_close}→{last_close}  ← CANDIDATE")
                scan_hits.append({
                    "month":  month_label,
                    "token":  candidate,
                    "offset": offset,
                    "bars":   len(bars),
                    "first":  str(bars[0]["date"].date()),
                    "last":   str(bars[-1]["date"].date()),
                })
                break
            else:
                pass  # silent: most will be wrong tokens or empty
        except Exception:
            time.sleep(0.35)
            pass

# ── Fetch 15/30/60-min for any confirmed tokens ───────────────────────────────
all_tokens = {**found_tokens}
for hit in scan_hits:
    key = f"{hit['month']}FUT_scan"
    all_tokens[key] = hit["token"]

if all_tokens:
    print()
    print("=== Fetching intraday data for confirmed tokens ===")
    for label, token in all_tokens.items():
        for interval in ["15minute", "30minute", "60minute"]:
            try:
                start_dt = datetime(2026, 1, 1)
                bars = kite.historical_data(
                    token, start_dt, END, interval, continuous=False, oi=False
                ) or []
                time.sleep(0.35)
                if bars:
                    csv_path = OUT / f"{label}_{interval.replace('minute','min')}.csv"
                    with csv_path.open("w", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=[
                            "date", "time", "open", "high", "low", "close", "volume"
                        ])
                        w.writeheader()
                        for b in bars:
                            dt = b["date"]
                            w.writerow({
                                "date": dt.strftime("%Y-%m-%d"),
                                "time": dt.strftime("%H:%M:%S"),
                                "open": b["open"], "high": b["high"],
                                "low": b["low"],   "close": b["close"],
                                "volume": b.get("volume", ""),
                            })
                    print(f"  {label} {interval}: {len(bars)} bars → {csv_path.name}")
                    break  # got finest interval; skip coarser ones
            except Exception as exc:
                print(f"  {label} {interval}: {exc}")
                time.sleep(0.35)
else:
    print()
    print("No expired tokens recovered via either approach.")
    print("Kite does not expose intraday data for delisted MCX futures.")
    print("For historical intraday data before March 2026, alternatives are:")
    print("  • True Data  (trudata.in)")
    print("  • iCharts    (icharts.in)")
    print("  • Upstox historical API (if you have an account)")
    print("  • Angel One SmartAPI historical endpoint")
