#!/usr/bin/env python3
"""IV snapshot from the last available closing prices (Jun 12, 2026)."""
from __future__ import annotations
import io, sys, math
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from pathlib import Path
from scipy.optimize import brentq
from scipy.stats import norm

DATA   = Path("data/ng_options")
EXPIRY = pd.Timestamp("2026-06-23 23:30:00", tz="Asia/Kolkata")
RFRATE = 0.065
STUDY_MEAN_IV = 51.1   # from analyse_iv_patterns.py


def b76(F: float, K: float, T: float, r: float, sigma: float, flag: str) -> float:
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-r * T)
    if flag == "c":
        return disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def calc_iv(price: float, F: float, K: float, T: float, r: float, flag: str) -> float | None:
    if price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    try:
        lo, hi = 0.01, 20.0
        if b76(F, K, T, r, lo, flag) - price > 0:
            return None
        if b76(F, K, T, r, hi, flag) - price < 0:
            return None
        return brentq(lambda s: b76(F, K, T, r, s, flag) - price, lo, hi, xtol=1e-6)
    except Exception:
        return None


# ── Futures: last bar on Jun 12 ───────────────────────────────────────────────
fut = pd.read_csv(DATA / "NG_futures_5min.csv")
fut["dt"] = pd.to_datetime(fut["date"] + " " + fut["time"]).dt.tz_localize("Asia/Kolkata")
fut_jun12 = fut[(fut["tradingsymbol"] == "NATURALGAS26JUNFUT") & (fut["date"] == "2026-06-12")]
last_fut  = fut_jun12.sort_values("dt").iloc[-1]
F   = float(last_fut["close"])
ts  = last_fut["dt"]
T   = (EXPIRY - ts).total_seconds() / (365.25 * 24 * 3600)
atm = round(round(F / 5) * 5, 1)

print("=" * 60)
print("  NG JUN26 — IV SNAPSHOT  (Jun 12, 2026 last bar)")
print("=" * 60)
print(f"  Last futures bar  : {ts.strftime('%Y-%m-%d %H:%M %Z')}")
print(f"  Futures price (F) : {F:.2f}")
print(f"  Expiry            : 2026-06-23 23:30 IST")
print(f"  Time to expiry    : {T * 365.25:.1f} calendar days")
print(f"  ATM strike        : {atm:.0f}")
print()

# ── Options: last bar on Jun 12 for each strike/type ─────────────────────────
dfs = [pd.read_csv(p) for p in sorted((DATA / "2026-06").glob("*_5min.csv"))]
opts = pd.concat(dfs, ignore_index=True)
opts["dt"] = pd.to_datetime(opts["date"] + " " + opts["time"]).dt.tz_localize("Asia/Kolkata")
last_opts = (
    opts[opts["date"] == "2026-06-12"]
    .sort_values("dt")
    .groupby(["strike", "option_type"])
    .last()
    .reset_index()
)

print(f"  Option bars from  : {last_opts['dt'].min().strftime('%H:%M')} "
      f"to {last_opts['dt'].max().strftime('%H:%M')}")
print()

# ── IV table ──────────────────────────────────────────────────────────────────
print(f"  {'Strike':>7}  {'Type':>4}  {'Close':>7}  {'IV':>7}  {'Moneyness'}")
print(f"  {'-'*7}  {'-'*4}  {'-'*7}  {'-'*7}  {'-'*12}")

results: list[dict] = []
for _, row in last_opts.sort_values(["strike", "option_type"]).iterrows():
    K     = float(row["strike"])
    price = float(row["close"])
    flag  = "c" if row["option_type"] == "CE" else "p"
    sigma = calc_iv(price, F, K, T, RFRATE, flag)

    if K < F - 2.5:
        money = "ITM (CE) / OTM (PE)"
    elif K > F + 2.5:
        money = "OTM (CE) / ITM (PE)"
    else:
        money = "<<< ATM >>>"

    iv_str = f"{sigma * 100:.1f}%" if sigma else "N/A   "
    marker = "  <-- ATM" if abs(K - atm) < 0.1 else ""
    print(f"  {K:>7.0f}  {row['option_type']:>4}  {price:>7.2f}  {iv_str:>7}  {money}{marker}")
    results.append({"strike": K, "type": row["option_type"], "price": price, "iv": sigma})

# ── ATM straddle summary ──────────────────────────────────────────────────────
atm_rows = [r for r in results if abs(r["strike"] - atm) < 0.1 and r["iv"]]
ce_row   = next((r for r in atm_rows if r["type"] == "CE"), None)
pe_row   = next((r for r in atm_rows if r["type"] == "PE"), None)

print()
print("=" * 60)
print(f"  ATM STRADDLE  (strike {atm:.0f})")
print("=" * 60)
if ce_row and pe_row:
    atm_iv    = (ce_row["iv"] + pe_row["iv"]) / 2 * 100
    straddle  = ce_row["price"] + pe_row["price"]
    diff      = atm_iv - STUDY_MEAN_IV
    regime    = "ELEVATED" if diff > 1.5 else "COMPRESSED" if diff < -1.5 else "near average"
    print(f"  CE close   : {ce_row['price']:.2f}   IV = {ce_row['iv']*100:.1f}%")
    print(f"  PE close   : {pe_row['price']:.2f}   IV = {pe_row['iv']*100:.1f}%")
    print(f"  Straddle   : {straddle:.2f}")
    print(f"  ATM IV     : {atm_iv:.1f}%  (avg CE + PE)")
    print(f"  Study mean : {STUDY_MEAN_IV}%  (Apr-Jun avg from 38 days)")
    print(f"  vs mean    : {diff:+.1f} pp  -->  {regime}")
    print()

    # What does today's IV suggest based on the pattern analysis?
    print("=" * 60)
    print("  CONTEXT: What to expect next")
    print("=" * 60)
    print(f"  Today is Friday Jun 13 (expiry in {int(T*365.25)} days).")
    print(f"  Based on the IV pattern study:")
    print(f"    * Friday mornings tend to be HIGH IV (52.7% avg in 09-11:30)")
    print(f"    * Friday afternoon declines through 14:00-16:30 (avg -0.9%)")
    print(f"    * Monday-Tuesday see continued contraction (Tue: -0.9% vs prev day)")
    print(f"    * With expiry Jun 23, entering the last-10-day window:")
    print(f"      theta acceleration will start overriding IV expansion signals")
    print(f"    * Watch 19:00-21:30 window for consistent IV contraction (-0.4% avg)")

print()
print("  [IV computed via Black-76; F = front-month futures close; r = 6.5%]")
