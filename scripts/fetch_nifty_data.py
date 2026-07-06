#!/usr/bin/env python3
"""
Fetch 5-min OHLC for NIFTY options + futures.
NIFTY now expires on TUESDAY.
Saves to data/nifty_options/:
  - futures_5min.csv
  - {YYYY-MM-DD}/{SYMBOL}_5min.csv  (grouped by expiry)
Run inside Docker:
  docker compose run --rm -v $(pwd)/scripts:/app/scripts bot python scripts/fetch_nifty_data.py
"""
from __future__ import annotations
import csv, time, sys, calendar
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, ".")
from app.kite_session import get_session_manager

kite    = get_session_manager().get_kite()
OUT     = Path("data/nifty_options")
OUT.mkdir(parents=True, exist_ok=True)

END_DT    = datetime(2026, 6, 12, 15, 30, 0)   # last completed trading day
START_DT  = datetime(2026, 3, 1)
N_STRIKES = 10    # ± strikes around ATM
CHUNK_DAYS = 60

def fetch_chunked(token, start, end, interval="5minute"):
    all_bars = []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), end)
        try:
            bars = kite.historical_data(token, cur, chunk_end, interval,
                                        continuous=False, oi=False) or []
            all_bars.extend(bars)
            time.sleep(0.38)
        except Exception as e:
            print(f"    chunk {cur.date()}–{chunk_end.date()} ERR: {e}")
            time.sleep(0.5)
        cur = chunk_end + timedelta(seconds=1)
    return all_bars

print("=" * 65)
print("  NIFTY — options + futures fetch (weekly + monthly)")
print("=" * 65)

# ── 1. Instruments ────────────────────────────────────────────────────────────
print("\n[1] Loading NFO instruments...")
nfo = kite.instruments("NFO")
time.sleep(1.0)

nf_all = [r for r in nfo if r.get("name") == "NIFTY"]
print(f"  NIFTY instruments: {len(nf_all):,}")

nf_futs = sorted([r for r in nf_all if r["instrument_type"] == "FUT"],
                 key=lambda x: x["expiry"])
nf_opts = [r for r in nf_all if r["instrument_type"] in ("CE","PE")]

lot_size = nf_futs[0]["lot_size"] if nf_futs else 65
print(f"  Lot size: {lot_size}")
for f in nf_futs:
    print(f"    {f['tradingsymbol']:30s}  token={f['instrument_token']}  expiry={f['expiry']}")

# All available option expiries
all_expiries = sorted({r["expiry"] for r in nf_opts})
print(f"\n  All available option expiries ({len(all_expiries)}): {all_expiries[:12]}")

# Classify expiry as monthly (last Tuesday of month) or weekly
def is_monthly_expiry(d: date) -> bool:
    """Last Tuesday of the month."""
    last_tue = max(w[1] for w in calendar.monthcalendar(d.year, d.month) if w[1])
    return d.day == last_tue

# Near-term expiries we'll actually fetch: weekly up to ~8 weeks out, monthly up to Dec 2026
near_expiries = [e for e in all_expiries if e <= date(2026, 8, 1)]
print("\n  Expiries to fetch:")
for e in near_expiries:
    print(f"    {e}  {'MONTHLY' if is_monthly_expiry(e) else 'weekly '}")

# ── 2. Futures (front-month) ──────────────────────────────────────────────────
print("\n[2] Fetching NIFTY futures 5-min (chunked)...")
fut_rows = []
fut_fields = ["date","time","open","high","low","close","volume","tradingsymbol","expiry"]

front_fut = nf_futs[0]  # JUN front-month
tok = front_fut["instrument_token"]
sym = front_fut["tradingsymbol"]
exp = front_fut["expiry"]
w_end = min(datetime(exp.year, exp.month, exp.day, 15, 30), END_DT)

print(f"  {sym}: {START_DT.date()} -> {w_end.date()}")
bars = fetch_chunked(tok, START_DT, w_end)
print(f"  {len(bars)} bars")
for b in bars:
    dt = b["date"]
    fut_rows.append({"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
                     "open": b["open"], "high": b["high"], "low": b["low"],
                     "close": b["close"], "volume": b.get("volume",""),
                     "tradingsymbol": sym, "expiry": str(exp)})

with (OUT / "futures_5min.csv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fut_fields); w.writeheader(); w.writerows(fut_rows)
print(f"  Saved {len(fut_rows)} rows -> futures_5min.csv")

# ── 3. Daily ATM ──────────────────────────────────────────────────────────────
import pandas as pd
fut_df = pd.DataFrame(fut_rows)
fut_df["close"] = fut_df["close"].astype(float)
fut_df["dt"] = pd.to_datetime(fut_df["date"] + " " + fut_df["time"])

daily_close: dict[str, float] = {}
for day, grp in fut_df.sort_values("dt").groupby("date"):
    daily_close[day] = float(grp.sort_values("time").iloc[-1]["close"])

print(f"\n  Trading days: {len(daily_close)}  |  range: {min(daily_close.values()):.0f}–{max(daily_close.values()):.0f}")

# ── 4. Strike interval ────────────────────────────────────────────────────────
print("\n[3] Discovering strike interval...")
sample_strikes = sorted({float(r["strike"]) for r in nf_opts if r.get("strike")})
diffs = sorted({round(sample_strikes[i+1]-sample_strikes[i],0)
                for i in range(min(100,len(sample_strikes)-1)) if sample_strikes[i+1]-sample_strikes[i]>0})
strike_interval = min(d for d in diffs if d>=25) if diffs else 50.0
print(f"  Strike interval: {strike_interval}")

# ── 5. Fetch options per expiry ───────────────────────────────────────────────
print("\n[4] Fetching options 5-min (chunked)...")
opt_fields = ["date","time","open","high","low","close","volume","oi","expiry","strike","option_type"]
total_rows = 0
skipped    = 0

by_expiry = defaultdict(list)
for r in nf_opts:
    by_expiry[r["expiry"]].append(r)

for exp_date in near_expiries:
    exp_dt  = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 30)
    w_end_o = min(exp_dt, END_DT)
    w_start_o = START_DT

    if w_end_o <= w_start_o:
        print(f"  {exp_date}: before start — skip")
        continue

    trading_days = [d for d in daily_close
                    if w_start_o.strftime("%Y-%m-%d") <= d <= w_end_o.strftime("%Y-%m-%d")]
    if not trading_days:
        print(f"  {exp_date}: no futures data — skip")
        continue

    exp_opts_list = by_expiry[exp_date]
    avail_strikes = sorted({float(r["strike"]) for r in exp_opts_list if r.get("strike")})

    atm_strikes: set[float] = set()
    for ds in trading_days:
        F = daily_close.get(ds, 0)
        if F <= 0: continue
        raw_atm = round(round(F / strike_interval) * strike_interval, 0)
        if not avail_strikes: continue
        idx = min(range(len(avail_strikes)), key=lambda i: abs(avail_strikes[i]-raw_atm))
        lo  = max(0, idx - N_STRIKES)
        hi  = min(len(avail_strikes), idx + N_STRIKES + 1)
        atm_strikes.update(avail_strikes[lo:hi])

    exp_type = "MONTHLY" if is_monthly_expiry(exp_date) else "weekly"
    print(f"  {exp_date} [{exp_type}]: {len(trading_days)} days, {len(atm_strikes)} strikes")

    exp_dir = OUT / str(exp_date)
    exp_dir.mkdir(parents=True, exist_ok=True)

    for strike in sorted(atm_strikes):
        for opt_type in ("CE", "PE"):
            matches = [r for r in exp_opts_list
                       if abs(float(r.get("strike",0)) - strike) < 0.01
                       and r["instrument_type"] == opt_type]
            if not matches:
                skipped += 1; continue

            opt      = matches[0]
            token    = opt["instrument_token"]
            sym_o    = opt["tradingsymbol"]
            csv_path = exp_dir / f"{sym_o}_5min.csv"
            if csv_path.exists():
                continue

            try:
                bars = fetch_chunked(token, w_start_o, w_end_o)
                if not bars:
                    skipped += 1; continue

                with csv_path.open("w", newline="") as fc:
                    wr = csv.DictWriter(fc, fieldnames=opt_fields); wr.writeheader()
                    for b in bars:
                        dt = b["date"]
                        wr.writerow({"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
                                     "open": b["open"], "high": b["high"], "low": b["low"],
                                     "close": b["close"], "volume": b.get("volume",""),
                                     "oi": b.get("oi",""), "expiry": str(exp_date),
                                     "strike": strike, "option_type": opt_type})
                total_rows += len(bars)
                print(f"    {sym_o}: {len(bars)} bars")
            except Exception as e:
                print(f"    {sym_o}: ERR {e}")
                skipped += 1; time.sleep(0.4)

print(f"\n  Total option bars: {total_rows:,}  |  skipped: {skipped}")
print(f"  Output: {OUT.resolve()}")
print("Done.")
