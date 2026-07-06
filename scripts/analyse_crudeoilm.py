#!/usr/bin/env python3
"""
CRUDEOILM intraday short straddle analysis:
  1. IV pattern by 2.5-hr window and day-of-week
  2. Per-window short straddle backtest (last 30 days)
  3. Focus on 22:00-23:25 window: all days + Mon/Tue/Fri
Run locally after fetching data:
  python scripts/analyse_crudeoilm.py
"""
from __future__ import annotations
import io, sys, math, warnings, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from pathlib import Path
from scipy.optimize import brentq
from scipy.stats import norm

DATA     = Path("data/crudeoilm_options")
LOT_SIZE = 10          # barrels per lot
RFRATE   = 0.065
DOW_NAMES = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
SL_PCT    = 0.50

WINDOWS = [
    ("09:00-11:30", "09:00:00", "11:30:00"),
    ("11:30-14:00", "11:30:00", "14:00:00"),
    ("14:00-16:30", "14:00:00", "16:30:00"),
    ("16:30-19:00", "16:30:00", "19:00:00"),
    ("19:00-21:30", "19:00:00", "21:30:00"),
    ("21:30-23:30", "21:30:00", "23:30:00"),
]

# ── Black-76 IV solver ────────────────────────────────────────────────────────
def b76(F, K, T, r, s, flag):
    d1 = (math.log(F/K) + 0.5*s*s*T) / (s*math.sqrt(T))
    d2 = d1 - s*math.sqrt(T)
    disc = math.exp(-r*T)
    if flag == "c": return disc*(F*norm.cdf(d1) - K*norm.cdf(d2))
    return disc*(K*norm.cdf(-d2) - F*norm.cdf(-d1))

def calc_iv(price, F, K, T, r, flag):
    if price <= 0 or F <= 0 or K <= 0 or T <= 0: return None
    try:
        lo, hi = 0.01, 20.0
        if b76(F, K, T, r, lo, flag) > price: return None
        if b76(F, K, T, r, hi, flag) < price: return None
        return brentq(lambda s: b76(F, K, T, r, s, flag) - price, lo, hi, xtol=1e-6)
    except Exception: return None

# ── Load data ────────────────────────────────────────────────────────────────
print("=" * 70)
print("  CRUDEOILM — IV Pattern + Short Straddle Backtest")
print("=" * 70)

if not DATA.exists():
    print(f"\nERROR: {DATA} does not exist. Run fetch_crudeoilm_data.py first.")
    sys.exit(1)

# Find most recent options expiry folder with data
opt_dirs = sorted([d for d in DATA.iterdir() if d.is_dir() and d.name[:4].isdigit()])
if not opt_dirs:
    print("ERROR: No options data found. Run fetch_crudeoilm_data.py first.")
    sys.exit(1)

print(f"\nFound option folders: {[d.name for d in opt_dirs]}")

# Load all option CSVs
dfs = []
for d in opt_dirs:
    for p in sorted(d.glob("*_5min.csv")):
        df = pd.read_csv(p)
        if not df.empty:
            dfs.append(df)

if not dfs:
    print("ERROR: No option CSV data found.")
    sys.exit(1)

opts = pd.concat(dfs, ignore_index=True)
opts["dt"] = pd.to_datetime(opts["date"] + " " + opts["time"]).dt.tz_localize("Asia/Kolkata")
opts["strike"] = opts["strike"].astype(float)
opts["close"]  = opts["close"].astype(float)
opts = opts[opts["close"] > 0].sort_values("dt").reset_index(drop=True)

# Load futures
fut_path = DATA / "futures_5min.csv"
if not fut_path.exists():
    print("ERROR: futures_5min.csv not found.")
    sys.exit(1)

fut = pd.read_csv(fut_path)
fut["dt"]    = pd.to_datetime(fut["date"] + " " + fut["time"]).dt.tz_localize("Asia/Kolkata")
fut["close"] = fut["close"].astype(float)
fut = fut.sort_values("dt").reset_index(drop=True)

# Use the most-traded futures symbol (most bars) for primary analysis
sym_counts = fut.groupby("tradingsymbol").size()
primary_sym = sym_counts.idxmax()
print(f"\nPrimary futures symbol: {primary_sym} ({sym_counts[primary_sym]} bars)")

# Use options from the most recent expiry with sufficient data
opt_expiry_counts = opts.groupby("expiry").size()
print(f"Options data by expiry:\n{opt_expiry_counts.to_string()}")
primary_expiry = opt_expiry_counts.idxmax()  # expiry with most data
print(f"\nUsing options expiry: {primary_expiry}")

opts_primary = opts[opts["expiry"].astype(str).str[:10] == str(primary_expiry)[:10]].copy()
print(f"Options rows for analysis: {len(opts_primary)}")

# Find futures that overlap with options period
opts_dates = set(opts_primary["date"].unique())
fut_dates  = set(fut["date"].unique())
common_days = sorted(opts_dates & fut_dates)
print(f"Common trading days: {len(common_days)}  ({common_days[0] if common_days else 'none'} to {common_days[-1] if common_days else 'none'})")

if len(common_days) < 5:
    print("ERROR: Not enough common days for analysis.")
    sys.exit(1)

# Find expiry datetime for Black-76
expiry_str = str(primary_expiry)[:10]
expiry_dt  = pd.Timestamp(expiry_str + " 23:30:00", tz="Asia/Kolkata")

# Use last 30 days
analysis_days = common_days[-30:]
print(f"\nAnalysis period: {analysis_days[0]} to {analysis_days[-1]} ({len(analysis_days)} days)")

# ── SECTION 1: IV Pattern Analysis ───────────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 1: IV PATTERNS BY WINDOW AND DAY-OF-WEEK")
print("=" * 70)

iv_records = []

for day in analysis_days:
    fut_day  = fut[(fut["date"] == day)].sort_values("dt")
    opts_day = opts_primary[opts_primary["date"] == day].sort_values("dt")
    if fut_day.empty or opts_day.empty:
        continue

    # For each 5-min bar, compute ATM IV
    for _, fbar in fut_day.iterrows():
        F    = float(fbar["close"])
        ts   = fbar["dt"]
        if F <= 0: continue

        # Strike interval from available strikes
        avail = sorted(opts_day["strike"].unique())
        if len(avail) < 2: continue
        diffs = [avail[i+1]-avail[i] for i in range(len(avail)-1)]
        si = min(d for d in diffs if d > 0) if diffs else 50
        atm = round(round(F / si) * si, 2)

        # T in years
        T = (expiry_dt - ts).total_seconds() / (365.25 * 24 * 3600)
        if T <= 0: continue

        # Get CE and PE at ATM for this bar
        bar_opts = opts_day[(opts_day["strike"] == atm) & (opts_day["time"] == fbar["time"])]
        ce_row = bar_opts[bar_opts["option_type"] == "CE"]
        pe_row = bar_opts[bar_opts["option_type"] == "PE"]
        if ce_row.empty or pe_row.empty: continue

        ce_price = float(ce_row.iloc[0]["close"])
        pe_price = float(pe_row.iloc[0]["close"])
        if ce_price <= 0 or pe_price <= 0: continue

        iv_ce = calc_iv(ce_price, F, atm, T, RFRATE, "c")
        iv_pe = calc_iv(pe_price, F, atm, T, RFRATE, "p")
        if iv_ce is None and iv_pe is None: continue

        ivs = [x for x in [iv_ce, iv_pe] if x is not None]
        iv_median = np.median(ivs) * 100  # convert to %

        hhmm = ts.hour * 60 + ts.minute
        win_label = None
        for wl, ws, we in WINDOWS:
            ws_m = int(ws[:2])*60 + int(ws[3:5])
            we_m = int(we[:2])*60 + int(we[3:5])
            if ws_m <= hhmm < we_m:
                win_label = wl; break

        iv_records.append({
            "date": day, "dt": ts, "F": F, "atm": atm,
            "iv": iv_median, "window": win_label,
            "dow": DOW_NAMES[datetime.date.fromisoformat(day).weekday()],
        })

iv_df = pd.DataFrame(iv_records)
iv_df = iv_df.dropna(subset=["iv","window"])

if iv_df.empty:
    print("  WARNING: No IV data computed. Skipping IV analysis.")
else:
    study_mean_iv = iv_df["iv"].median()
    print(f"\n  Study median IV: {study_mean_iv:.1f}%  (across {len(iv_df)} bars, {len(analysis_days)} days)")

    # Window stats
    win_order = {w[0]: i for i, w in enumerate(WINDOWS)}
    iv_df["worder"] = iv_df["window"].map(win_order)
    wstats = iv_df.groupby("window")["iv"].agg(["mean","median","std","count"]).reset_index()
    wstats["worder"] = wstats["window"].map(win_order)
    wstats = wstats.sort_values("worder")

    print(f"\n  {'Window':>15}  {'Mean IV':>9}  {'Median':>8}  {'Bars':>5}")
    print("  " + "-" * 50)
    for _, r in wstats.iterrows():
        print(f"  {r['window']:>15}  {r['mean']*1:>8.1f}%  {r['median']:>7.1f}%  {int(r['count']):>5}")

    # Delta vs prior window per day
    win_day = iv_df.groupby(["date","window"])["iv"].median().reset_index()
    win_day["worder"] = win_day["window"].map(win_order)
    win_day = win_day.sort_values(["date","worder"])
    win_day["prev_iv"] = win_day.groupby("date")["iv"].shift(1)
    win_day["delta"]   = win_day["iv"] - win_day["prev_iv"]

    dstats = win_day.dropna(subset=["delta"]).groupby("window").agg(
        mean_d=("delta","mean"), n=("delta","count")).reset_index()
    dstats["worder"] = dstats["window"].map(win_order)
    dstats = dstats.sort_values("worder")

    print(f"\n  Delta vs prior window (negative = IV contraction):")
    print(f"  {'Window':>15}  {'Avg delta (pp)':>16}  {'N':>4}  {'Direction'}")
    print("  " + "-" * 55)
    for _, r in dstats.iterrows():
        tag = "CONTRACTION" if r["mean_d"] < 0 else "expansion"
        print(f"  {r['window']:>15}  {r['mean_d']:>+15.3f}  {int(r['n']):>4}  {tag}")

    # DoW breakdown per window
    print(f"\n  DoW x Window IV matrix (median IV %):")
    dow_win = iv_df.groupby(["dow","window"])["iv"].median().unstack("window")
    dow_order = ["Mon","Tue","Wed","Thu","Fri"]
    win_cols  = [w[0] for w in WINDOWS if w[0] in dow_win.columns]
    dow_win = dow_win.reindex(index=[d for d in dow_order if d in dow_win.index], columns=win_cols)
    header = f"  {'':>5}" + "".join(f"  {c:>13}" for c in win_cols)
    print(header)
    print("  " + "-" * (len(header)-2))
    for dow in dow_win.index:
        row_str = f"  {dow:>5}"
        for c in win_cols:
            v = dow_win.loc[dow, c]
            row_str += f"  {v:>12.1f}%" if not pd.isna(v) else f"  {'--':>12}"
        print(row_str)

# ── SECTION 2: Window Backtest ────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 2: WINDOW BACKTEST (50% per-leg SL)")
print("=" * 70)

def trade_window(day, ws, we, fd, od):
    ef = fd[fd["time"] >= ws]
    if ef.empty: ef = fd
    if ef.empty: return None
    F = float(ef.iloc[0]["close"])

    avail = sorted(od["strike"].unique())
    if len(avail) < 2: return None
    diffs = [avail[i+1]-avail[i] for i in range(len(avail)-1)]
    si = min(d for d in diffs if d > 0) if diffs else 50
    atm = round(round(F / si) * si, 2)
    atm_u = min(avail, key=lambda s: abs(s - atm))

    ce_all = od[(od["strike"]==atm_u)&(od["option_type"]=="CE")].sort_values("time")
    pe_all = od[(od["strike"]==atm_u)&(od["option_type"]=="PE")].sort_values("time")
    if ce_all.empty or pe_all.empty: return None

    cee = ce_all[ce_all["time"]>=ws]; pee = pe_all[pe_all["time"]>=ws]
    if cee.empty or pee.empty: return None

    ce_in = float(cee.iloc[0]["close"]); pe_in = float(pee.iloc[0]["close"])
    et = cee.iloc[0]["time"]
    if ce_in <= 0.5 or pe_in <= 0.5: return None

    ce_sl = ce_in*(1+SL_PCT); pe_sl = pe_in*(1+SL_PCT)

    ci = ce_all[(ce_all["time"]>et)&(ce_all["time"]<=we)].sort_values("time").set_index("time")[["high","close"]].rename(columns={"high":"ch","close":"cc"})
    pi = pe_all[(pe_all["time"]>et)&(pe_all["time"]<=we)].sort_values("time").set_index("time")[["high","close"]].rename(columns={"high":"ph","close":"pc"})
    bars = ci.join(pi, how="outer").sort_index().ffill()

    lc=ce_in; lp=pe_in; cx=px=None; ex="EOW"; sl_l=None

    for bt, row in bars.iterrows():
        ch=row.get("ch",np.nan); ph=row.get("ph",np.nan)
        cc=row.get("cc",np.nan); pc=row.get("pc",np.nan)
        if not np.isnan(cc): lc=cc
        if not np.isnan(pc): lp=pc
        ch_hit=(not np.isnan(ch)) and ch>=ce_sl
        ph_hit=(not np.isnan(ph)) and ph>=pe_sl
        if ch_hit or ph_hit:
            cx=ce_sl if ch_hit else lc; px=pe_sl if ph_hit else lp
            ex="SL"; sl_l="BOTH" if (ch_hit and ph_hit) else "CE" if ch_hit else "PE"; break
        if bt>=we: cx=lc; px=lp; break

    if cx is None:
        lce=ce_all[ce_all["time"]<=we]; lpe=pe_all[pe_all["time"]<=we]
        cx=float(lce.iloc[-1]["close"]) if not lce.empty else ce_in
        px=float(lpe.iloc[-1]["close"]) if not lpe.empty else pe_in

    pnl = (ce_in - cx) + (pe_in - px)
    return {"F":F,"atm":atm_u,"ce_in":ce_in,"pe_in":pe_in,"ce_out":cx,"pe_out":px,
            "pnl_u":pnl,"pnl_lot":pnl*LOT_SIZE,
            "exit":ex+(f"({sl_l})" if sl_l else "")}

all_results: dict[str, list] = {w[0]: [] for w in WINDOWS}

for day in analysis_days:
    dow  = DOW_NAMES[datetime.date.fromisoformat(day).weekday()]
    fd   = fut[fut["date"]==day].sort_values("time")
    od   = opts_primary[opts_primary["date"]==day].sort_values("time")
    for wl, ws, we in WINDOWS:
        r = trade_window(day, ws, we, fd, od)
        if r:
            r["date"]=day; r["dow"]=dow; r["window"]=wl
            all_results[wl].append(r)

print(f"\n  {'Window':>15}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  {'Win%':>6}  {'Avg P&L/lot':>13}  {'Total/lot':>11}")
print("  " + "-" * 80)
for wl, _, _ in WINDOWS:
    rows = all_results[wl]
    if not rows: continue
    n    = len(rows)
    wins = sum(1 for r in rows if r["pnl_u"]>0)
    sls  = sum(1 for r in rows if "SL" in r["exit"])
    tot  = sum(r["pnl_lot"] for r in rows)
    print(f"  {wl:>15}  {n:>5}  {wins:>5}  {sls:>4}  {wins/n*100:>5.0f}%  {tot/n:>+13,.0f}  {tot:>+11,.0f}")

# DoW breakdown for each window
print()
for wl, _, _ in WINDOWS:
    rows = all_results[wl]
    if not rows: continue
    dow_g: dict[str,list] = {}
    for r in rows: dow_g.setdefault(r["dow"],[]).append(r["pnl_lot"])
    parts = []
    for d in ["Mon","Tue","Wed","Thu","Fri"]:
        vals=dow_g.get(d,[])
        if vals:
            parts.append(f"{d}:{sum(vals):+,.0f}({len(vals)}d)")
    print(f"  {wl:>15}: {' | '.join(parts)}")

# ── SECTION 3: 22:00-23:25 focused analysis ──────────────────────────────────
print("\n" + "=" * 70)
print("  SECTION 3: 22:00-23:25 WINDOW — DETAILED")
print("=" * 70)

for label, ws, we, day_filter in [
    ("ALL DAYS",        "22:00:00", "23:25:00", None),
    ("Mon/Tue/Fri ONLY","22:00:00", "23:25:00", {"Mon","Tue","Fri"}),
]:
    print(f"\n  --- {label} ---")
    rows = []
    for day in analysis_days:
        dow = DOW_NAMES[datetime.date.fromisoformat(day).weekday()]
        if day_filter and dow not in day_filter:
            continue
        fd = fut[fut["date"]==day].sort_values("time")
        od = opts_primary[opts_primary["date"]==day].sort_values("time")
        r  = trade_window(day, ws, we, fd, od)
        if r:
            r["date"]=day; r["dow"]=dow
            rows.append(r)

    if not rows:
        print("  No data.")
        continue

    print(f"  {'Date':>12} {'DoW':>4} {'ATM':>7} {'CE_in':>7} {'PE_in':>7} {'CE_out':>7} {'PE_out':>7} {'PnL/u':>8} {'PnL/lot':>9} {'Exit'}")
    print("  " + "-" * 95)
    cum=0
    for r in rows:
        cum += r["pnl_lot"]
        w = "WIN " if r["pnl_u"]>0 else "LOSS"
        print(f"  {r['date']}  {r['dow']:>4}  {r['atm']:>7.1f}  {r['ce_in']:>7.2f}  {r['pe_in']:>7.2f}  "
              f"{r['ce_out']:>7.2f}  {r['pe_out']:>7.2f}  {r['pnl_u']:>+8.2f}  {r['pnl_lot']:>+9.0f}  "
              f"{r['exit']:<14} {w}  cum:{cum:+,}")

    n=len(rows); wins=sum(1 for r in rows if r["pnl_u"]>0)
    sls=sum(1 for r in rows if "SL" in r["exit"])
    tot=sum(r["pnl_lot"] for r in rows)
    print(f"\n  Trades:{n}  Wins:{wins}({wins/n*100:.0f}%)  SL:{sls}  Gross:Rs {tot:+,}/lot  Avg:Rs {tot/n:+,.0f}/trade")

    # DoW breakdown
    dow_g: dict[str,list] = {}
    for r in rows: dow_g.setdefault(r["dow"],[]).append(r["pnl_lot"])
    print(f"\n  {'DoW':>5} {'Days':>5} {'Wins':>5} {'SL':>4} {'Total':>11} {'Avg':>9}")
    for d in ["Mon","Tue","Wed","Thu","Fri"]:
        vals=dow_g.get(d,[])
        if not vals: continue
        dw=sum(1 for v in vals if v>0)
        dt=sum(vals)
        ds=sum(1 for r in rows if "SL" in r["exit"] and r["dow"]==d)
        print(f"  {d:>5} {len(vals):>5} {dw:>5} {ds:>4} {dt:>+11,} {dt/len(vals):>+9,.0f}")

    print(f"\n  Slippage impact:")
    for slip in [0.20, 0.40, 0.60]:
        net = tot - slip*LOT_SIZE*n
        print(f"    Rs {slip:.2f}/unit/trade -> Net Rs {net:+,.0f}  (Rs {net/n:+,.0f}/trade)")

print()
print("  [Black-76 IV | r=6.5% | SL=50% per-leg | ATM = nearest strike to F]")
