#!/usr/bin/env python3
"""
IV contraction / expansion analysis for MCX NATURALGAS JUN26 options.

Inputs  (local):
  data/ng_options/2026-06/*_5min.csv  — option OHLC
  data/ng_options/NG_futures_5min.csv — underlying 5-min price

Outputs  (printed + saved):
  data/ng_options/iv_analysis.csv     — per-bar IV series
  data/ng_options/iv_summary.txt      — pattern report
"""

from __future__ import annotations

import io
import sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

warnings.filterwarnings("ignore")

DATA  = Path("data/ng_options")
EXPIRY_DATE = pd.Timestamp("2026-06-23 23:30:00", tz="Asia/Kolkata")
RISK_FREE   = 0.065          # India 10-yr G-sec proxy
MIN_PRICE   = 0.25           # ignore bars with option price below this (illiquid)
STRIKE_STEP = 5.0

# MCX NATURALGAS session: 09:00 – 23:30 IST
SESSION_START = 9
SESSION_END   = 23  # bars up to 23:30 included

WINDOWS = [
    ("09:00-11:30", 9,  11, 30),
    ("11:30-14:00", 11, 14,  0),
    ("14:00-16:30", 14, 16, 30),
    ("16:30-19:00", 16, 19,  0),
    ("19:00-21:30", 19, 21, 30),
    ("21:30-23:30", 21, 23, 30),
]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# ─────────────────────────────────────────────────────────────────────────────
# Black-76 IV solver
# ─────────────────────────────────────────────────────────────────────────────

def _b76_price(F: float, K: float, T: float, r: float, sigma: float, flag: str) -> float:
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-r * T)
    if flag == "c":
        return disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    else:
        return disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def implied_vol(price: float, F: float, K: float, T: float, r: float, flag: str) -> float | None:
    if price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (F - K) if flag == "c" else (K - F))
    if price < intrinsic * 0.99:
        return None
    try:
        lo, hi = 0.01, 20.0
        f_lo = _b76_price(F, K, T, r, lo, flag) - price
        f_hi = _b76_price(F, K, T, r, hi, flag) - price
        if f_lo * f_hi > 0:
            return None
        return brentq(lambda s: _b76_price(F, K, T, r, s, flag) - price, lo, hi, xtol=1e-6)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Load data
# ─────────────────────────────────────────────────────────────────────────────

print("Loading futures 5-min data…")
fut = pd.read_csv(DATA / "NG_futures_5min.csv")
fut["datetime"] = pd.to_datetime(fut["date"] + " " + fut["time"])
fut["datetime"] = fut["datetime"].dt.tz_localize("Asia/Kolkata")
# Use front-month (JUNFUT) which has the most data
fut = fut[fut["tradingsymbol"] == "NATURALGAS26JUNFUT"].copy()
fut = fut.set_index("datetime")[["close"]].rename(columns={"close": "F"})
print(f"  Futures: {len(fut)} bars  {fut.index.min().date()} to {fut.index.max().date()}")

print("Loading JUN26 option CSVs…")
opt_dfs = []
for csv_path in sorted((DATA / "2026-06").glob("*_5min.csv")):
    df = pd.read_csv(csv_path)
    opt_dfs.append(df)
opts = pd.concat(opt_dfs, ignore_index=True)
opts["datetime"] = pd.to_datetime(opts["date"] + " " + opts["time"])
opts["datetime"] = opts["datetime"].dt.tz_localize("Asia/Kolkata")
opts["strike"]   = opts["strike"].astype(float)
print(f"  Options: {len(opts)} bars  {opts['datetime'].min().date()} to {opts['datetime'].max().date()}")
print(f"  Strikes: {sorted(opts['strike'].unique())}")

# ─────────────────────────────────────────────────────────────────────────────
# Compute ATM IV at each 5-min bar
# ─────────────────────────────────────────────────────────────────────────────

print("\nCalculating ATM IV…")
records = []

for ts, row_fut in fut.iterrows():
    F = row_fut["F"]
    if F <= 0:
        continue

    # ATM strike = nearest multiple of STRIKE_STEP
    atm = round(round(F / STRIKE_STEP) * STRIKE_STEP, 1)

    # time to expiry in years
    T = (EXPIRY_DATE - ts).total_seconds() / (365.25 * 24 * 3600)
    if T <= 0:
        continue

    # grab ATM CE and PE option bar at this timestamp
    mask = (
        (opts["datetime"] == ts) &
        (opts["strike"] == atm)
    )
    atm_bars = opts[mask]

    iv_vals = []
    for _, orow in atm_bars.iterrows():
        price = float(orow["close"])
        if price < MIN_PRICE:
            continue
        flag = "c" if orow["option_type"] == "CE" else "p"
        iv = implied_vol(price, F, atm, T, RISK_FREE, flag)
        if iv is not None and 0.01 < iv < 15.0:
            iv_vals.append(iv * 100)  # store as %

    if not iv_vals:
        # fallback: try ±1 strike
        for strike_offset in [-STRIKE_STEP, STRIKE_STEP]:
            nearby = atm + strike_offset
            mask2 = (opts["datetime"] == ts) & (opts["strike"] == nearby)
            for _, orow in opts[mask2].iterrows():
                price = float(orow["close"])
                if price < MIN_PRICE:
                    continue
                flag = "c" if orow["option_type"] == "CE" else "p"
                iv = implied_vol(price, F, nearby, T, RISK_FREE, flag)
                if iv is not None and 0.01 < iv < 15.0:
                    iv_vals.append(iv * 100)

    if iv_vals:
        records.append({
            "datetime":   ts,
            "F":          F,
            "atm_strike": atm,
            "iv":         float(np.median(iv_vals)),
            "iv_count":   len(iv_vals),
        })

iv_df = pd.DataFrame(records).set_index("datetime").sort_index()
iv_df.to_csv(DATA / "iv_analysis.csv")
print(f"  IV calculated for {len(iv_df)} bars  (out of {len(fut)} futures bars)")
if iv_df.empty:
    print("  ERROR: no IV values computed — check option/futures timestamp alignment")
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Feature engineering
# ─────────────────────────────────────────────────────────────────────────────

iv_df["hour"]    = iv_df.index.hour
iv_df["minute"]  = iv_df.index.minute
iv_df["hhmm"]    = iv_df["hour"] * 60 + iv_df["minute"]
iv_df["dow"]     = iv_df.index.dayofweek          # 0=Mon
iv_df["dow_name"]= iv_df.index.day_name()
iv_df["date"]    = iv_df.index.date

# Filter to session hours only
iv_df = iv_df[
    (iv_df["hour"] >= SESSION_START) &
    (iv_df["hhmm"] <= SESSION_END * 60 + 30)
].copy()

# ── 2-3 hour windows ─────────────────────────────────────────────────────────
def assign_window(hhmm: int) -> str:
    for label, h_start, h_end, m_end in WINDOWS:
        start_min = h_start * 60
        end_min   = h_end * 60 + m_end
        if start_min <= hhmm < end_min:
            return label
    return "other"

iv_df["window"] = iv_df["hhmm"].apply(assign_window)

# ── Previous-window IV delta ──────────────────────────────────────────────────
# For each bar, compute the mean IV of its window and the previous window (same day)
window_means = (
    iv_df.groupby(["date", "window"])["iv"]
    .mean()
    .reset_index()
    .rename(columns={"iv": "win_mean_iv"})
)
window_order = {w[0]: i for i, w in enumerate(WINDOWS)}
window_means["win_order"] = window_means["window"].map(window_order)
window_means = window_means.sort_values(["date", "win_order"])
window_means["prev_win_iv"] = (
    window_means.groupby("date")["win_mean_iv"].shift(1)
)
window_means["delta_vs_prev_window"] = window_means["win_mean_iv"] - window_means["prev_win_iv"]

# ── Previous-day IV delta ─────────────────────────────────────────────────────
day_means = iv_df.groupby("date")["iv"].mean().rename("day_mean_iv")
day_means_df = day_means.reset_index()
day_means_df["prev_day_iv"] = day_means_df["day_mean_iv"].shift(1)
day_means_df["delta_vs_prev_day"] = day_means_df["day_mean_iv"] - day_means_df["prev_day_iv"]

# ─────────────────────────────────────────────────────────────────────────────
# Analysis & report
# ─────────────────────────────────────────────────────────────────────────────

lines = []
sep   = "=" * 62

def h(title: str) -> None:
    lines.append("")
    lines.append(sep)
    lines.append(f"  {title}")
    lines.append(sep)

def row(label: str, val: str) -> None:
    lines.append(f"  {label:<38s} {val}")

h("OVERALL IV STATS")
row("Mean IV",   f"{iv_df['iv'].mean():.1f}%")
row("Median IV", f"{iv_df['iv'].median():.1f}%")
row("Min IV",    f"{iv_df['iv'].min():.1f}%")
row("Max IV",    f"{iv_df['iv'].max():.1f}%")
row("Std-dev",   f"{iv_df['iv'].std():.1f}%")
row("Date range",f"{iv_df.index.min().date()} to {iv_df.index.max().date()}")
row("Bars used", f"{len(iv_df)}")

# ── 1. By hour ────────────────────────────────────────────────────────────────
h("IV BY HOUR OF DAY (mean ± std, MCX session 09:00–23:30 IST)")
hourly = iv_df.groupby("hour")["iv"].agg(["mean", "std", "count"])
hourly_sorted = hourly.sort_values("mean")
lowest_hour  = hourly_sorted.index[0]
highest_hour = hourly_sorted.index[-1]

lines.append(f"  {'Hour':>6}  {'Mean IV':>8}  {'Std':>6}  {'Bars':>5}  Bar")
lines.append(f"  {'-'*6}  {'-'*8}  {'-'*6}  {'-'*5}  ---")
for hr, r in hourly.iterrows():
    bar = "#" * int(r["mean"] / 2)
    lines.append(f"  {hr:>5}h  {r['mean']:>7.1f}%  {r['std']:>5.1f}%  {int(r['count']):>5}  {bar}")
lines.append(f"\n  to Lowest IV hour : {lowest_hour:02d}:00  ({hourly.loc[lowest_hour,'mean']:.1f}%)")
lines.append(f"  to Highest IV hour: {highest_hour:02d}:00  ({hourly.loc[highest_hour,'mean']:.1f}%)")

# ── 2. By day of week ─────────────────────────────────────────────────────────
h("IV BY DAY OF WEEK")
dow_iv = iv_df.groupby(["dow", "dow_name"])["iv"].agg(["mean", "std", "count"]).reset_index()
dow_iv = dow_iv.sort_values("dow")
lines.append(f"  {'Day':<12}  {'Mean IV':>8}  {'Std':>6}  {'Bars':>5}")
lines.append(f"  {'-'*12}  {'-'*8}  {'-'*6}  {'-'*5}")
for _, r in dow_iv.iterrows():
    lines.append(f"  {r['dow_name']:<12}  {r['mean']:>7.1f}%  {r['std']:>5.1f}%  {int(r['count']):>5}")
lowest_dow  = dow_iv.loc[dow_iv['mean'].idxmin(), 'dow_name']
highest_dow = dow_iv.loc[dow_iv['mean'].idxmax(), 'dow_name']
lines.append(f"\n  to Lowest IV day : {lowest_dow}")
lines.append(f"  to Highest IV day: {highest_dow}")

# ── 3. By 2.5-hour window ─────────────────────────────────────────────────────
h("IV BY 2.5-HOUR WINDOW (mean ± std)")
win_iv = iv_df.groupby("window")["iv"].agg(["mean", "std", "count"])
win_iv["order"] = win_iv.index.map(window_order)
win_iv = win_iv.sort_values("order")
lines.append(f"  {'Window':<16}  {'Mean IV':>8}  {'Std':>6}  {'Bars':>5}")
lines.append(f"  {'-'*16}  {'-'*8}  {'-'*6}  {'-'*5}")
for win, r in win_iv.iterrows():
    lines.append(f"  {win:<16}  {r['mean']:>7.1f}%  {r['std']:>5.1f}%  {int(r['count']):>5}")

# ── 4. IV change vs previous window ──────────────────────────────────────────
h("IV DELTA: EACH WINDOW vs PREVIOUS WINDOW (avg across all days)")
wm_agg = (
    window_means.dropna(subset=["delta_vs_prev_window"])
    .groupby("window")["delta_vs_prev_window"]
    .agg(["mean", "std", "count"])
)
wm_agg["order"] = wm_agg.index.map(window_order)
wm_agg = wm_agg.sort_values("order")
lines.append(f"  {'Window':<16}  {'dIV(pp)':>9}  {'Std':>6}  Interpretation")
lines.append(f"  {'-'*16}  {'-'*9}  {'-'*6}  {'-'*20}")
for win, r in wm_agg.iterrows():
    direction = "CONTRACTION v" if r["mean"] < -0.3 else "EXPANSION ^" if r["mean"] > 0.3 else "flat ~"
    lines.append(f"  {win:<16}  {r['mean']:>+8.1f}%  {r['std']:>5.1f}%  {direction}")

# ── 5. IV change vs previous day ─────────────────────────────────────────────
h("IV DELTA: EACH DAY OF WEEK vs PREVIOUS DAY (avg)")
iv_df2 = iv_df.copy()
iv_df2["date_str"] = iv_df2["date"].astype(str)
day_dow = iv_df2.groupby(["date_str", "dow_name"])["iv"].mean().reset_index()
day_dow = day_dow.sort_values("date_str")
day_dow["prev_iv"] = day_dow["iv"].shift(1)
day_dow["delta"]   = day_dow["iv"] - day_dow["prev_iv"]
dow_delta = day_dow.dropna(subset=["delta"]).groupby("dow_name")["delta"].agg(["mean", "std"])
dow_order = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
dow_delta["order"] = dow_delta.index.map(dow_order)
dow_delta = dow_delta.sort_values("order")
lines.append(f"  {'Day':<12}  {'dIV(pp)':>9}  {'Std':>6}  Interpretation")
lines.append(f"  {'-'*12}  {'-'*9}  {'-'*6}  {'-'*20}")
for day, r in dow_delta.iterrows():
    direction = "CONTRACTION v" if r["mean"] < -0.3 else "EXPANSION ^" if r["mean"] > 0.3 else "flat ~"
    lines.append(f"  {day:<12}  {r['mean']:>+8.1f}%  {r['std']:>5.1f}%  {direction}")

# ── 6. Window × Day heatmap (mean IV) ────────────────────────────────────────
h("HEATMAP: MEAN IV  (window rows × day-of-week cols)")
hm = iv_df.groupby(["window", "dow_name"])["iv"].mean().unstack("dow_name")
day_cols = [d for d in DAYS if d in hm.columns]
hm = hm[day_cols]
hm["order"] = hm.index.map(window_order)
hm = hm.sort_values("order").drop(columns="order")
lines.append(f"  {'':16s}" + "".join(f"  {d[:3]:>7}" for d in day_cols))
lines.append(f"  {'-'*16}" + "  " + "  ".join(["-" * 7] * len(day_cols)))
for win, r in hm.iterrows():
    vals = "  ".join(f"{r[d]:>6.1f}%" if d in r.index and not pd.isna(r[d]) else "     N/A" for d in day_cols)
    lines.append(f"  {win:<16}  {vals}")

# ── 7. Top contraction and expansion windows ──────────────────────────────────
h("TOP CONTRACTION WINDOWS (window × day combinations)")
# For each day × window pair compute mean iv and delta from previous window
combo = iv_df.groupby(["dow_name", "window"])["iv"].mean().reset_index()
combo["win_order"] = combo["window"].map(window_order)
combo["dow_order"] = combo["dow_name"].map(dow_order)
combo = combo.sort_values(["dow_order", "win_order"])
combo["prev_iv"] = combo.groupby("dow_name")["iv"].shift(1)
combo["delta"]   = combo["iv"] - combo["prev_iv"]
combo_clean = combo.dropna(subset=["delta"]).sort_values("delta")

lines.append("  Biggest IV drops (vs prior 2.5-hr window, avg across all weeks):")
lines.append(f"  {'Day':<12}  {'Window':<16}  {'Mean IV':>8}  {'dIV':>8}")
for _, r in combo_clean.head(8).iterrows():
    lines.append(f"  {r['dow_name']:<12}  {r['window']:<16}  {r['iv']:>7.1f}%  {r['delta']:>+7.1f}%")

lines.append("\n  Biggest IV spikes (vs prior 2.5-hr window):")
for _, r in combo_clean.tail(8).iloc[::-1].iterrows():
    lines.append(f"  {r['dow_name']:<12}  {r['window']:<16}  {r['iv']:>7.1f}%  {r['delta']:>+7.1f}%")

# ── 8. Key findings summary ───────────────────────────────────────────────────
h("KEY FINDINGS SUMMARY")

best_contraction_win = wm_agg["mean"].idxmin()
best_contraction_val = wm_agg.loc[best_contraction_win, "mean"]
best_expansion_win   = wm_agg["mean"].idxmax()
best_expansion_val   = wm_agg.loc[best_expansion_win, "mean"]

best_contraction_dow = dow_delta["mean"].idxmin()
best_expansion_dow   = dow_delta["mean"].idxmax()

best_combo_row       = combo_clean.iloc[0]
worst_combo_row      = combo_clean.iloc[-1]

lines += [
    f"  IV CONTRACTION (intraday):",
    f"    • Strongest window  : {best_contraction_win}  (avg {best_contraction_val:+.1f}% vs prior window)",
    f"    • Best day          : {best_contraction_dow}  (avg {dow_delta.loc[best_contraction_dow,'mean']:+.1f}% vs prev day)",
    f"    • Best combination  : {best_combo_row['dow_name']} {best_combo_row['window']}  (dIV {best_combo_row['delta']:+.1f}%)",
    f"    • Lowest IV hour    : {lowest_hour:02d}:00",
    "",
    f"  IV EXPANSION (intraday):",
    f"    • Strongest window  : {best_expansion_win}  (avg {best_expansion_val:+.1f}% vs prior window)",
    f"    • Best day          : {best_expansion_dow}  (avg {dow_delta.loc[best_expansion_dow,'mean']:+.1f}% vs prev day)",
    f"    • Best combination  : {worst_combo_row['dow_name']} {worst_combo_row['window']}  (dIV {worst_combo_row['delta']:+.1f}%)",
    f"    • Highest IV hour   : {highest_hour:02d}:00",
    "",
    f"  DATA QUALITY NOTE:",
    f"    JUN26 options data spans {iv_df.index.min().date()} to {iv_df.index.max().date()}",
    f"    ({len(iv_df['date'].unique())} trading days, {len(iv_df)} 5-min bars with valid IV)",
    f"    IV computed as Black-76 median(ATM CE, ATM PE); risk-free=6.5%",
]

# ── Print and save ─────────────────────────────────────────────────────────────
report = "\n".join(lines)
print(report)

summary_path = DATA / "iv_summary.txt"
summary_path.write_text(report, encoding="utf-8")
print(f"\nReport saved to {summary_path}")
