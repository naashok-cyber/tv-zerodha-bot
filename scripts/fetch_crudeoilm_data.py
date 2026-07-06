#!/usr/bin/env python3
"""
Fetch 5-min OHLC for CRUDEOILM options + futures from Kite Connect.
Saves to data/crudeoilm_options/:
  - futures_5min.csv            : front-month continuous 5-min
  - {EXPIRY}/{SYMBOL}_5min.csv  : per-option 5-min bars
Run inside Docker:
  docker compose run --rm -v $(pwd)/scripts:/app/scripts bot python scripts/fetch_crudeoilm_data.py
"""
from __future__ import annotations
import csv, time, sys
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, ".")
from app.kite_session import get_session_manager

kite = get_session_manager().get_kite()
OUT  = Path("data/crudeoilm_options")
OUT.mkdir(parents=True, exist_ok=True)

END_DT   = datetime(2026, 6, 12, 23, 59, 59)   # last trading day
START_DT = datetime(2026, 3, 1)                  # ~90 days back
UNDERLYING = "CRUDEOILM"
LOT_SIZE   = 10        # barrels
N_STRIKES  = 10        # ± strikes around ATM to fetch

print("=" * 65)
print(f"  {UNDERLYING} options + futures fetch")
print("=" * 65)

# ── 1. Discover all CRUDEOILM instruments ────────────────────────────────────
print("\n[1] Fetching MCX instrument list...")
instr = kite.instruments("MCX")
time.sleep(0.5)

# Futures
futs = sorted(
    [r for r in instr if r["name"] == UNDERLYING and r["instrument_type"] == "FUT"],
    key=lambda x: x["expiry"],
)
print(f"  Futures found: {len(futs)}")
for f in futs:
    print(f"    {f['tradingsymbol']:30s}  token={f['instrument_token']}  expiry={f['expiry']}")

# Options
all_opts = [r for r in instr if r["name"] == UNDERLYING and r["instrument_type"] in ("CE","PE","OPT")]
# Also check MCX-OPT
all_opts += [r for r in instr if r["name"] == UNDERLYING and r["segment"] == "MCX-OPT"]
# Deduplicate by token
seen = set()
opts_dedup = []
for o in all_opts:
    if o["instrument_token"] not in seen:
        seen.add(o["instrument_token"])
        opts_dedup.append(o)
all_opts = opts_dedup

# Group by expiry
from collections import defaultdict
by_expiry: dict[date, list] = defaultdict(list)
for o in all_opts:
    by_expiry[o["expiry"]].append(o)

print(f"  Option expiries found: {sorted(by_expiry.keys())}")

# ── 2. Futures: find front-month for each period ─────────────────────────────
print("\n[2] Fetching 5-min futures data...")

futures_rows: list[dict] = []
fut_fields = ["date","time","open","high","low","close","volume","tradingsymbol","expiry"]

for fut in futs:
    token = fut["instrument_token"]
    sym   = fut["tradingsymbol"]
    exp   = fut["expiry"]

    # Determine relevant date window for this contract (it's front-month ~30 days before expiry)
    exp_dt = datetime(exp.year, exp.month, exp.day)
    w_end  = min(exp_dt, END_DT)
    w_start = max(START_DT, exp_dt - timedelta(days=60))

    print(f"  {sym}: {w_start.date()} -> {w_end.date()}", end="  ")
    try:
        bars = kite.historical_data(token, w_start, w_end, "5minute", continuous=False, oi=False) or []
        time.sleep(0.4)
        if bars:
            print(f"{len(bars)} bars")
            for b in bars:
                dt = b["date"]
                futures_rows.append({
                    "date": dt.strftime("%Y-%m-%d"),
                    "time": dt.strftime("%H:%M:%S"),
                    "open": b["open"], "high": b["high"],
                    "low": b["low"],   "close": b["close"],
                    "volume": b.get("volume",""),
                    "tradingsymbol": sym,
                    "expiry": str(exp),
                })
        else:
            print("0 bars")
    except Exception as e:
        print(f"ERROR: {e}")
        time.sleep(0.4)

fut_path = OUT / "futures_5min.csv"
with fut_path.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fut_fields)
    w.writeheader()
    w.writerows(futures_rows)
print(f"  Saved {len(futures_rows)} rows -> {fut_path.name}")

# ── 3. Determine ATM from daily futures close per expiry ──────────────────────
print("\n[3] Determining ATM strikes per expiry...")

import pandas as pd
fut_df = pd.DataFrame(futures_rows)
if fut_df.empty:
    print("  No futures data! Cannot continue.")
    sys.exit(1)

fut_df["dt"] = pd.to_datetime(fut_df["date"] + " " + fut_df["time"])
fut_df["close"] = fut_df["close"].astype(float)

# Daily close per expiry symbol
daily_atm: dict[str, set[float]] = {}
for sym, grp in fut_df.groupby("tradingsymbol"):
    daily_close = grp.groupby("date")["close"].last()
    atms = set()
    for c in daily_close:
        # Discover strike interval dynamically from available options
        atms.add(c)
    daily_atm[sym] = atms

# Figure out strike interval from the options data
all_strikes = sorted({float(o["strike"]) for o in all_opts if o.get("strike")})
print(f"  Sample strikes: {all_strikes[:15]}...")
if len(all_strikes) >= 2:
    diffs = sorted(set(round(all_strikes[i+1]-all_strikes[i], 1) for i in range(min(20,len(all_strikes)-1))))
    strike_interval = min(d for d in diffs if d > 0)
else:
    strike_interval = 50.0  # fallback for CRUDEOILM
print(f"  Strike interval detected: {strike_interval}")

# For each expiry, find ATM strikes to fetch
expiry_atm_strikes: dict[date, set[float]] = {}
for exp_date, exp_opts in by_expiry.items():
    exp_dt = datetime(exp_date.year, exp_date.month, exp_date.day)
    # Get futures for this expiry period
    relevant_sym = None
    for fut in futs:
        if fut["expiry"] == exp_date:
            relevant_sym = fut["tradingsymbol"]
            break
    if not relevant_sym:
        # Use nearest expiry futures
        closest = min(futs, key=lambda f: abs((datetime(f["expiry"].year, f["expiry"].month, f["expiry"].day) - exp_dt).days))
        relevant_sym = closest["tradingsymbol"]

    sym_data = fut_df[fut_df["tradingsymbol"] == relevant_sym]
    if sym_data.empty:
        continue

    # ATM from each day's close
    daily_closes = sym_data.groupby("date")["close"].last()
    atm_set: set[float] = set()
    avail_strikes = sorted({float(o["strike"]) for o in exp_opts if o.get("strike")})
    for c in daily_closes:
        raw_atm = round(round(c / strike_interval) * strike_interval, 2)
        # Find ±N_STRIKES around ATM from available
        idx = min(range(len(avail_strikes)), key=lambda i: abs(avail_strikes[i]-raw_atm))
        lo = max(0, idx - N_STRIKES)
        hi = min(len(avail_strikes), idx + N_STRIKES + 1)
        atm_set.update(avail_strikes[lo:hi])
    expiry_atm_strikes[exp_date] = atm_set
    print(f"  {exp_date}: {len(atm_set)} strikes to fetch  (interval={strike_interval})")

# ── 4. Fetch options 5-min bars ───────────────────────────────────────────────
print("\n[4] Fetching options 5-min bars...")

opt_fields = ["date","time","open","high","low","close","volume","oi","expiry","strike","option_type"]
total_rows = 0
skipped = 0

for exp_date, target_strikes in sorted(expiry_atm_strikes.items()):
    exp_dir = OUT / str(exp_date)[:7].replace("-","")   # e.g. 202606
    exp_dir_named = OUT / f"{exp_date.year}-{exp_date.month:02d}"
    exp_dir_named.mkdir(parents=True, exist_ok=True)

    exp_dt  = datetime(exp_date.year, exp_date.month, exp_date.day)
    w_end   = min(exp_dt + timedelta(days=1), END_DT)
    w_start = max(START_DT, exp_dt - timedelta(days=90))

    exp_opts_list = by_expiry[exp_date]

    for strike in sorted(target_strikes):
        for opt_type in ("CE", "PE"):
            # find matching instrument
            matches = [o for o in exp_opts_list
                       if abs(float(o.get("strike",0)) - strike) < 0.01
                       and (o.get("instrument_type","").upper() in (opt_type,"OPT"))
                       and (o.get("option_type","").upper() == opt_type if o.get("option_type") else True)]

            # fallback: segment filter
            if not matches:
                matches = [o for o in exp_opts_list
                           if abs(float(o.get("strike",0)) - strike) < 0.01]
                matches = [o for o in matches if opt_type.lower() in o.get("tradingsymbol","").lower()]

            if not matches:
                skipped += 1
                continue

            opt = matches[0]
            token = opt["instrument_token"]
            sym   = opt["tradingsymbol"]
            csv_path = exp_dir_named / f"{sym}_5min.csv"

            if csv_path.exists():
                continue   # already fetched

            try:
                bars = kite.historical_data(token, w_start, w_end, "5minute", continuous=False, oi=False) or []
                time.sleep(0.35)
                if not bars:
                    skipped += 1
                    continue

                with csv_path.open("w", newline="") as fcsv:
                    wr = csv.DictWriter(fcsv, fieldnames=opt_fields)
                    wr.writeheader()
                    for b in bars:
                        dt = b["date"]
                        wr.writerow({
                            "date": dt.strftime("%Y-%m-%d"),
                            "time": dt.strftime("%H:%M:%S"),
                            "open": b["open"], "high": b["high"],
                            "low": b["low"],   "close": b["close"],
                            "volume": b.get("volume",""),
                            "oi":     b.get("oi",""),
                            "expiry": str(exp_date),
                            "strike": strike,
                            "option_type": opt_type,
                        })
                total_rows += len(bars)
                print(f"  {sym}: {len(bars)} bars -> {csv_path.name}")
            except Exception as e:
                print(f"  {sym}: ERROR {e}")
                skipped += 1
                time.sleep(0.4)

print(f"\n  Total option bars saved : {total_rows}")
print(f"  Skipped (no data)       : {skipped}")
print(f"  Output dir              : {OUT.resolve()}")
print("\nDone.")
