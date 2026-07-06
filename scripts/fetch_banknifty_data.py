#!/usr/bin/env python3
"""
Fetch 5-min OHLC for BANKNIFTY monthly options + futures.
Saves to data/banknifty_options/:
  - futures_5min.csv             : front-month BANKNIFTY futures 5-min
  - {YYYY-MM-DD}/{SYMBOL}_5min.csv : options by expiry date
Run inside Docker:
  docker compose run --rm -v $(pwd)/scripts:/app/scripts bot python scripts/fetch_banknifty_data.py
"""
from __future__ import annotations
import csv, time, sys
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, ".")
from app.kite_session import get_session_manager

kite    = get_session_manager().get_kite()
OUT     = Path("data/banknifty_options")
OUT.mkdir(parents=True, exist_ok=True)

END_DT    = datetime(2026, 6, 12, 15, 30, 0)
START_DT  = datetime(2026, 3, 1)
N_STRIKES = 10   # ± strikes around ATM per day
CHUNK_DAYS = 60  # Kite 5-min limit is ~100 days; use 60 to be safe

def fetch_chunked(token, start, end, interval="5minute"):
    """Fetch historical data in 60-day chunks to respect Kite API limits."""
    all_bars = []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), end)
        try:
            bars = kite.historical_data(token, cur, chunk_end, interval, continuous=False, oi=False) or []
            all_bars.extend(bars)
            time.sleep(0.4)
        except Exception as e:
            print(f"  chunk {cur.date()}–{chunk_end.date()} ERROR: {e}")
            time.sleep(0.5)
        cur = chunk_end + timedelta(seconds=1)
    return all_bars

print("=" * 65)
print("  BANKNIFTY — monthly options + futures fetch")
print("=" * 65)

# ── 1. NFO instrument list ───────────────────────────────────────────────────
print("\n[1] Loading NFO instruments...")
nfo = kite.instruments("NFO")
time.sleep(1.0)
print(f"  Total NFO instruments: {len(nfo):,}")

bn_all = [r for r in nfo if r.get("name") == "BANKNIFTY"]
print(f"  BANKNIFTY instruments : {len(bn_all):,}")

# ── 2. Futures ───────────────────────────────────────────────────────────────
bn_futs = sorted([r for r in bn_all if r["instrument_type"] == "FUT"],
                 key=lambda x: x["expiry"])
print(f"\n  Futures:")
for f in bn_futs:
    print(f"    {f['tradingsymbol']:35s}  token={f['instrument_token']}  expiry={f['expiry']}")

lot_size = bn_futs[0]["lot_size"] if bn_futs else 30
print(f"  Lot size: {lot_size}")

# ── 3. Monthly option expiries available ────────────────────────────────────
bn_opts = [r for r in bn_all if r["instrument_type"] in ("CE","PE")]
expiry_set = sorted({r["expiry"] for r in bn_opts})
print(f"\n  Available option expiries: {expiry_set}")

# ── 4. Fetch futures 5-min (chunked) ────────────────────────────────────────
print("\n[2] Fetching BANKNIFTY futures 5-min (chunked)...")
fut_rows = []
fut_fields = ["date","time","open","high","low","close","volume","tradingsymbol","expiry"]

# Use front-month futures (JUN) for the full period
front_fut = bn_futs[0]  # earliest expiry = front month for our window
tok = front_fut["instrument_token"]
sym = front_fut["tradingsymbol"]
exp = front_fut["expiry"]
exp_dt = datetime(exp.year, exp.month, exp.day, 15, 30)
w_end   = min(exp_dt, END_DT)

print(f"  {sym}: {START_DT.date()} -> {w_end.date()} (chunked)")
bars = fetch_chunked(tok, START_DT, w_end)
print(f"    {len(bars)} total bars")
for b in bars:
    dt = b["date"]
    fut_rows.append({"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
                     "open": b["open"], "high": b["high"], "low": b["low"],
                     "close": b["close"], "volume": b.get("volume",""),
                     "tradingsymbol": sym, "expiry": str(exp)})

with (OUT / "futures_5min.csv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fut_fields); w.writeheader(); w.writerows(fut_rows)
print(f"  Saved {len(fut_rows)} rows -> futures_5min.csv")

# ── 5. Daily ATM from futures ────────────────────────────────────────────────
import pandas as pd
fut_df = pd.DataFrame(fut_rows)
if fut_df.empty:
    print("No futures data! Exiting.")
    sys.exit(1)

fut_df["close"] = fut_df["close"].astype(float)
fut_df["dt"]    = pd.to_datetime(fut_df["date"] + " " + fut_df["time"])

daily_close: dict[str, float] = {}
for day, grp in fut_df.sort_values("dt").groupby("date"):
    daily_close[day] = float(grp.sort_values("time").iloc[-1]["close"])

print(f"\n  Trading days with futures data: {len(daily_close)}")
print(f"  Price range: {min(daily_close.values()):.0f} – {max(daily_close.values()):.0f}")

# ── 6. Discover strike interval ──────────────────────────────────────────────
print("\n[3] Building option fetch plan...")
sample_strikes = sorted({float(r["strike"]) for r in bn_opts if r.get("strike")})
diffs = sorted({round(sample_strikes[i+1]-sample_strikes[i],0)
                for i in range(min(100,len(sample_strikes)-1)) if sample_strikes[i+1]-sample_strikes[i]>0})
strike_interval = min(d for d in diffs if d>=50) if diffs else 100.0
print(f"  Strike interval: {strike_interval}")

# ── 7. For each expiry, fetch options (chunked) ──────────────────────────────
opt_fields = ["date","time","open","high","low","close","volume","oi","expiry","strike","option_type"]
total_opt_rows = 0
skipped = 0

by_expiry = defaultdict(list)
for r in bn_opts:
    by_expiry[r["expiry"]].append(r)

for exp_date in expiry_set:
    exp_dt  = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 30)
    w_end   = min(exp_dt, END_DT)
    w_start = START_DT

    if w_end <= w_start:
        print(f"  {exp_date}: expiry before start — skip")
        continue

    trading_days = [d for d in daily_close
                    if w_start.strftime("%Y-%m-%d") <= d <= w_end.strftime("%Y-%m-%d")]
    if not trading_days:
        print(f"  {exp_date}: no futures data in range — skip")
        continue

    exp_opts      = by_expiry[exp_date]
    avail_strikes = sorted({float(r["strike"]) for r in exp_opts if r.get("strike")})

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

    print(f"  {exp_date}: {len(trading_days)} days, {len(atm_strikes)} strikes  "
          f"(range {min(atm_strikes):.0f}–{max(atm_strikes):.0f})")

    exp_dir = OUT / str(exp_date)
    exp_dir.mkdir(parents=True, exist_ok=True)

    for strike in sorted(atm_strikes):
        for opt_type in ("CE", "PE"):
            matches = [r for r in exp_opts
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
                bars = fetch_chunked(token, w_start, w_end)
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
                total_opt_rows += len(bars)
                print(f"    {sym_o}: {len(bars)} bars")
            except Exception as e:
                print(f"    {sym_o}: ERROR {e}")
                skipped += 1; time.sleep(0.4)

print(f"\n  Total option bars: {total_opt_rows:,}  |  skipped: {skipped}")
print(f"  Output: {OUT.resolve()}")
print("Done.")
