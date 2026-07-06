#!/usr/bin/env python3
"""
NIFTY options — IV patterns + windowed straddle backtest.
Monthly: JUN30 expiry (49 trading days, Apr-Jun)
Weekly : JUN16 + JUN23 expiries combined (~3-4 weeks each)
Session: 09:15–15:30 IST  |  Lot: 65  |  SL: 50% per-leg  |  BSM
Expiry day: TUESDAY (NIFTY shifted from Thu to Tue)
"""
from __future__ import annotations
import io, sys, math, warnings, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import datetime
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.optimize import brentq
from scipy.stats import norm

DATA     = Path("data/nifty_options")
LOT_SIZE = 65
SL_PCT   = 0.50
r_rate   = 0.065   # risk-free ~6.5%
q        = 0.0     # index, dividends embedded in futures

WINDOWS = [
    ("09:15-10:45", "09:15:00", "10:45:00"),
    ("10:45-12:15", "10:45:00", "12:15:00"),
    ("12:15-13:45", "12:15:00", "13:45:00"),
    ("13:45-15:30", "13:45:00", "15:30:00"),
]
DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

# ── BSM helpers ───────────────────────────────────────────────────────────────
def bsm_price(F, K, T, r, q, sigma, flag):
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0: return 0.0
    d1 = (math.log(F/K) + (r - q + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    N  = norm.cdf
    if flag == "CE":
        return F*math.exp(-q*T)*N(d1) - K*math.exp(-r*T)*N(d2)
    return K*math.exp(-r*T)*N(-d2) - F*math.exp(-q*T)*N(-d1)

def solve_iv(F, K, T, r, q, mkt, flag):
    if T <= 1e-6 or mkt <= 0 or F <= 0: return np.nan
    intrinsic = max(0, (F-K) if flag=="CE" else (K-F))
    if mkt <= intrinsic*1.001: return np.nan
    try:
        return brentq(lambda s: bsm_price(F,K,T,r,q,s,flag) - mkt, 0.01, 5.0, xtol=1e-4, maxiter=50)
    except: return np.nan

# ── Load futures ──────────────────────────────────────────────────────────────
def load_futures():
    fut = pd.read_csv(DATA / "futures_5min.csv")
    fut["dt"]    = pd.to_datetime(fut["date"] + " " + fut["time"]).dt.tz_localize("Asia/Kolkata")
    fut["close"] = fut["close"].astype(float)
    return fut[fut["tradingsymbol"] == "NIFTY26JUNFUT"].sort_values("dt").reset_index(drop=True)

# ── Load options from one or more expiry dirs ─────────────────────────────────
def load_opts(expiry_dates: list[str]) -> pd.DataFrame:
    dfs = []
    for ed in expiry_dates:
        d = DATA / ed
        if not d.exists(): continue
        for p in sorted(d.glob("*_5min.csv")):
            if p.stat().st_size == 0: continue
            df = pd.read_csv(p)
            df["expiry_label"] = ed
            dfs.append(df)
    if not dfs: return pd.DataFrame()
    opts = pd.concat(dfs, ignore_index=True)
    opts["dt"]     = pd.to_datetime(opts["date"] + " " + opts["time"]).dt.tz_localize("Asia/Kolkata")
    opts["strike"] = opts["strike"].astype(float)
    return opts.sort_values("dt").reset_index(drop=True)

# ── IV window analysis ────────────────────────────────────────────────────────
def iv_window_analysis(label, fut, opts, expiry_date_str, common_days):
    expiry_date = datetime.date.fromisoformat(expiry_date_str)
    iv_records  = []

    for day in common_days:
        fut_day  = fut[fut["date"] == day].sort_values("time")
        opts_day = opts[opts["date"] == day].sort_values("time")
        if opts_day.empty: continue

        T_yrs  = max((expiry_date - datetime.date.fromisoformat(day)).days / 365.0, 1/365)
        dow_s  = DOW[datetime.date.fromisoformat(day).weekday()]

        for win_label, win_start, win_end in WINDOWS:
            win_fut = fut_day[(fut_day["time"] >= win_start) & (fut_day["time"] <= win_end)]
            if win_fut.empty: continue
            F_bar = win_fut["close"].mean()

            avail = sorted(opts_day["strike"].unique())
            if not avail: continue
            atm = min(avail, key=lambda s: abs(s - F_bar))

            ce = opts_day[(opts_day["strike"]==atm) & (opts_day["option_type"]=="CE")]
            pe = opts_day[(opts_day["strike"]==atm) & (opts_day["option_type"]=="PE")]
            wce = ce[(ce["time"]>=win_start)&(ce["time"]<=win_end)]
            wpe = pe[(pe["time"]>=win_start)&(pe["time"]<=win_end)]
            if wce.empty or wpe.empty: continue

            F_s = float(win_fut.iloc[0]["close"])
            F_e = float(win_fut.iloc[-1]["close"])
            fce = wce.iloc[0]; lce = wce.iloc[-1]
            fpe = wpe.iloc[0]; lpe = wpe.iloc[-1]

            iv_s = np.nanmean([solve_iv(F_s,atm,T_yrs,r_rate,q,float(fce["close"]),"CE"),
                               solve_iv(F_s,atm,T_yrs,r_rate,q,float(fpe["close"]),"PE")])
            iv_e = np.nanmean([solve_iv(F_e,atm,T_yrs,r_rate,q,float(lce["close"]),"CE"),
                               solve_iv(F_e,atm,T_yrs,r_rate,q,float(lpe["close"]),"PE")])

            str_s = float(fce["close"]) + float(fpe["close"])
            str_e = float(lce["close"]) + float(lpe["close"])
            iv_records.append({
                "date": day, "dow": dow_s, "window": win_label,
                "F": F_bar, "atm": atm,
                "iv_start": iv_s*100 if not np.isnan(iv_s) else np.nan,
                "iv_end":   iv_e*100 if not np.isnan(iv_e) else np.nan,
                "iv_delta": (iv_e-iv_s)*100 if not (np.isnan(iv_s) or np.isnan(iv_e)) else np.nan,
                "str_start": str_s, "str_end": str_e,
                "str_pct": (str_e-str_s)/str_s*100 if str_s>0 else np.nan,
            })

    df = pd.DataFrame(iv_records)
    print(f"\n{'='*100}")
    print(f"  {label}  —  IV PATTERNS BY WINDOW")
    print(f"{'='*100}")
    print(f"\n  {'Window':>15}  {'Days':>5}  {'Avg IV':>8}  {'IV Δ (pp)':>10}  "
          f"{'Avg Strd':>9}  {'Strd Δ%':>8}  Character")
    print("  " + "-"*80)
    for win_label,_,_ in WINDOWS:
        sub = df[df["window"]==win_label].dropna(subset=["iv_delta","str_pct"])
        if sub.empty: continue
        n = len(sub)
        avg_iv  = sub["iv_start"].mean()
        avg_id  = sub["iv_delta"].mean()
        avg_ss  = sub["str_start"].mean()
        avg_sp  = sub["str_pct"].mean()
        char = "CONTRACT" if avg_id < -0.3 else ("EXPAND" if avg_id > 0.3 else "flat")
        print(f"  {win_label:>15}  {n:>5}  {avg_iv:>7.1f}%  {avg_id:>+10.2f}pp  "
              f"{avg_ss:>9.1f}  {avg_sp:>+8.1f}%  {char}")

    print(f"\n  DoW × Window (Strd Δ%):")
    print(f"  {'Window':>15}", end="")
    for d in ["Mon","Tue","Wed","Thu","Fri"]: print(f"  {d:>8}", end="")
    print()
    print("  " + "-"*55)
    for win_label,_,_ in WINDOWS:
        sub = df[df["window"]==win_label]
        print(f"  {win_label:>15}", end="")
        for d in ["Mon","Tue","Wed","Thu","Fri"]:
            dsub = sub[sub["dow"]==d]["str_pct"].dropna()
            val = f"{dsub.mean():>+8.1f}%" if not dsub.empty else f"{'—':>9}"
            print(f"  {val}", end="")
        print()
    return df

# ── Trade window simulation ───────────────────────────────────────────────────
def trade_window(day, win_start, win_end, fut_day, opts_day):
    ef = fut_day[fut_day["time"] >= win_start]
    if ef.empty: return None
    F = float(ef.iloc[0]["close"])
    avail = sorted(opts_day["strike"].unique())
    if not avail: return None
    atm = min(avail, key=lambda s: abs(s - F))

    ce_all = opts_day[(opts_day["strike"]==atm) & (opts_day["option_type"]=="CE")].sort_values("time")
    pe_all = opts_day[(opts_day["strike"]==atm) & (opts_day["option_type"]=="PE")].sort_values("time")
    if ce_all.empty or pe_all.empty: return None

    ce_e = ce_all[ce_all["time"] >= win_start]
    pe_e = pe_all[pe_all["time"] >= win_start]
    if ce_e.empty or pe_e.empty: return None

    ce_entry   = float(ce_e.iloc[0]["close"])
    pe_entry   = float(pe_e.iloc[0]["close"])
    entry_time = ce_e.iloc[0]["time"]
    if ce_entry <= 0.5 or pe_entry <= 0.5: return None

    ce_sl = ce_entry*(1+SL_PCT); pe_sl = pe_entry*(1+SL_PCT)

    ce_i = ce_all[(ce_all["time"]>entry_time)&(ce_all["time"]<=win_end)].sort_values("time")
    pe_i = pe_all[(pe_all["time"]>entry_time)&(pe_all["time"]<=win_end)].sort_values("time")

    ci = ce_i.set_index("time")[["high","close"]].rename(columns={"high":"ce_h","close":"ce_c"})
    pi = pe_i.set_index("time")[["high","close"]].rename(columns={"high":"pe_h","close":"pe_c"})
    bars = ci.join(pi, how="outer").sort_index().ffill()

    last_ce = ce_entry; last_pe = pe_entry
    ce_exit = pe_exit = None; exit_reason = "EOW"; sl_leg = None

    for bt, row in bars.iterrows():
        ch = row.get("ce_h",np.nan); ph = row.get("pe_h",np.nan)
        cc = row.get("ce_c",np.nan); pc = row.get("pe_c",np.nan)
        if not np.isnan(cc): last_ce = cc
        if not np.isnan(pc): last_pe = pc
        ce_hit = (not np.isnan(ch)) and ch >= ce_sl
        pe_hit = (not np.isnan(ph)) and ph >= pe_sl
        if ce_hit or pe_hit:
            ce_exit = ce_sl if ce_hit else last_ce
            pe_exit = pe_sl if pe_hit else last_pe
            exit_reason = "SL"
            sl_leg = "BOTH" if (ce_hit and pe_hit) else ("CE" if ce_hit else "PE")
            break
        if bt >= win_end:
            ce_exit = last_ce; pe_exit = last_pe; break

    if ce_exit is None:
        lb_ce = ce_all[ce_all["time"] <= win_end]
        lb_pe = pe_all[pe_all["time"] <= win_end]
        ce_exit = float(lb_ce.iloc[-1]["close"]) if not lb_ce.empty else ce_entry
        pe_exit = float(lb_pe.iloc[-1]["close"]) if not lb_pe.empty else pe_entry

    unit_pnl = (ce_entry - ce_exit) + (pe_entry - pe_exit)
    return {
        "day": day, "dow": DOW[datetime.date.fromisoformat(day).weekday()],
        "F": F, "atm": atm,
        "ce_in": ce_entry, "pe_in": pe_entry, "str_in": ce_entry+pe_entry,
        "ce_out": ce_exit, "pe_out": pe_exit,
        "unit_pnl": unit_pnl, "lot_pnl": unit_pnl*LOT_SIZE,
        "exit": exit_reason + (f"({sl_leg})" if sl_leg else ""),
        "sl_leg": sl_leg,
    }

# ── Window backtest ───────────────────────────────────────────────────────────
def run_window_backtest(label, fut, opts, common_days, show_detail=True):
    all_results = {w[0]: [] for w in WINDOWS}
    for day in common_days:
        fd = fut[fut["date"]==day].sort_values("time")
        od = opts[opts["date"]==day].sort_values("time")
        for wl, ws, we in WINDOWS:
            r = trade_window(day, ws, we, fd, od)
            if r:
                r["window"] = wl
                all_results[wl].append(r)

    print(f"\n{'='*100}")
    print(f"  {label}  —  WINDOWED SHORT STRADDLE BACKTEST  (SL=50% per-leg, Lot={LOT_SIZE})")
    print(f"{'='*100}\n")
    print(f"  {'Window':>15}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  "
          f"{'WinRate':>8}  {'Avg Strd':>9}  {'Avg PnL/lot':>12}  {'Total PnL':>12}")
    print("  " + "-"*95)
    for wl,_,_ in WINDOWS:
        rows = all_results[wl]
        if not rows: continue
        n = len(rows); wins = sum(1 for r in rows if r["unit_pnl"]>0)
        sls = sum(1 for r in rows if "SL" in r["exit"])
        total = sum(r["lot_pnl"] for r in rows); avg = total/n
        avgs  = sum(r["str_in"] for r in rows)/n
        wr    = wins/n*100
        print(f"  {wl:>15}  {n:>5}  {wins:>5}  {sls:>4}  "
              f"{wr:>7.0f}%  {avgs:>9.1f}  {avg:>+12,.0f}  {total:>+12,.0f}")

    if show_detail:
        for wl,_,_ in WINDOWS:
            rows = all_results[wl]
            if not rows: continue
            print(f"\n  {'─'*105}\n  Window: {wl}\n  {'─'*105}")
            print(f"  {'Date':>12}  {'DoW':>4}  {'ATM':>6}  {'Strd_in':>8}  "
                  f"{'CE_in':>7}  {'PE_in':>7}  {'CE_out':>7}  {'PE_out':>7}  "
                  f"{'PnL/u':>7}  {'PnL/lot':>9}  {'Exit':<16}")
            print("  " + "-"*100)
            running = 0
            for r in rows:
                running += r["lot_pnl"]
                w = "WIN " if r["unit_pnl"] > 0 else "LOSS"
                print(f"  {r['day']}  {r['dow']:>4}  {r['atm']:>6.0f}  "
                      f"{r['str_in']:>8.1f}  {r['ce_in']:>7.1f}  {r['pe_in']:>7.1f}  "
                      f"{r['ce_out']:>7.1f}  {r['pe_out']:>7.1f}  "
                      f"{r['unit_pnl']:>+7.1f}  {r['lot_pnl']:>+9.0f}  "
                      f"{r['exit']:<16}  {w}  cum:{running:+,.0f}")

    print(f"\n  DoW × Window:")
    print(f"  {'Window':>15}  {'DoW':>4}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  "
          f"{'WinRate':>8}  {'Avg PnL':>10}  {'Total':>10}")
    print("  " + "-"*80)
    for wl,_,_ in WINDOWS:
        rows = all_results[wl]
        if not rows: continue
        dg: dict[str,list] = {}
        for r in rows: dg.setdefault(r["dow"],[]).append(r)
        for d in ["Mon","Tue","Wed","Thu","Fri"]:
            dr = dg.get(d,[])
            if not dr: continue
            dn = len(dr); dw = sum(1 for r in dr if r["unit_pnl"]>0)
            ds = sum(1 for r in dr if "SL" in r["exit"]); dt = sum(r["lot_pnl"] for r in dr)
            print(f"  {wl:>15}  {d:>4}  {dn:>5}  {dw:>5}  {ds:>4}  "
                  f"{dw/dn*100:>7.0f}%  {dt/dn:>+10,.0f}  {dt:>+10,.0f}")

    return all_results

# ── Full-day backtest ─────────────────────────────────────────────────────────
def full_day_backtest(label, fut, opts, common_days):
    results = []
    for day in common_days:
        fd = fut[fut["date"]==day].sort_values("time")
        od = opts[opts["date"]==day].sort_values("time")
        r  = trade_window(day, "09:15:00", "15:30:00", fd, od)
        if r: results.append(r)

    if not results: return

    print(f"\n{'='*100}")
    print(f"  {label}  —  FULL DAY (09:15→15:30) SHORT STRADDLE")
    print(f"{'='*100}\n")
    print(f"  {'Date':>12}  {'DoW':>4}  {'ATM':>6}  {'Strd_in':>8}  "
          f"{'CE_in':>7}  {'PE_in':>7}  {'CE_out':>7}  {'PE_out':>7}  "
          f"{'PnL/u':>7}  {'PnL/lot':>9}  {'Exit':<16}")
    print("  " + "-"*105)
    cumul = 0
    for r in results:
        cumul += r["lot_pnl"]
        w = "WIN " if r["unit_pnl"] > 0 else "LOSS"
        print(f"  {r['day']}  {r['dow']:>4}  {r['atm']:>6.0f}  {r['str_in']:>8.1f}  "
              f"{r['ce_in']:>7.1f}  {r['pe_in']:>7.1f}  {r['ce_out']:>7.1f}  "
              f"{r['pe_out']:>7.1f}  {r['unit_pnl']:>+7.1f}  {r['lot_pnl']:>+9.0f}  "
              f"{r['exit']:<16}  {w}  cum:{cumul:+,.0f}")

    n = len(results); wins = sum(1 for r in results if r["unit_pnl"]>0)
    sls = sum(1 for r in results if "SL" in r["exit"])
    print(f"\n  Days:{n}  Wins:{wins} ({wins/n*100:.0f}%)  SL:{sls}")
    print(f"  Avg Strd in : {sum(r['str_in'] for r in results)/n:.1f}")
    print(f"  Gross P&L   : Rs {cumul:+,.0f} / lot")
    print(f"  Avg/day     : Rs {cumul/n:+,.0f} / lot")
    print(f"\n  Slippage (₹/unit/leg, both legs both ways):")
    for slip in [2,5,10,15]:
        net = cumul - slip*2*n
        print(f"    Rs {slip}/unit -> Net Rs {net:+,.0f}  ({net/n:+.0f}/day)")

    print(f"\n  DoW:")
    print(f"  {'DoW':>5}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  {'Avg Strd':>9}  {'Avg PnL':>9}  {'Total':>10}")
    print(f"  {'-'*60}")
    dg: dict[str,list] = {}
    for r in results: dg.setdefault(r["dow"],[]).append(r)
    for d in ["Mon","Tue","Wed","Thu","Fri"]:
        dr = dg.get(d,[])
        if not dr: continue
        dn = len(dr); dw = sum(1 for r in dr if r["unit_pnl"]>0)
        ds = sum(1 for r in dr if "SL" in r["exit"]); dt = sum(r["lot_pnl"] for r in dr)
        avgs = sum(r["str_in"] for r in dr)/dn
        print(f"  {d:>5}  {dn:>5}  {dw:>5}  {ds:>4}  {avgs:>9.1f}  {dt/dn:>+9,.0f}  {dt:>+10,.0f}")

# ═══════════════════════════════════════════════════════════════════════════════
print("=" * 100)
print("  NIFTY — Short Straddle Study  |  Monthly (Jun30) + Weekly (Jun16, Jun23)")
print(f"  Lot={LOT_SIZE}  |  SL=50% per-leg  |  Session 09:15-15:30  |  Expiry day=Tuesday")
print("=" * 100)

fut = load_futures()
print(f"\n  Futures: {len(fut):,} bars  ({fut['date'].nunique()} days)  "
      f"range {fut['close'].min():.0f}–{fut['close'].max():.0f}")

# ──────────────────────────────────────────────────────────────────────────────
# PART 1: MONTHLY (June 30 expiry)
# ──────────────────────────────────────────────────────────────────────────────
opts_monthly = load_opts(["2026-06-30"])
common_monthly = sorted(set(fut["date"]) & set(opts_monthly["date"]))
print(f"\n  Monthly (Jun30): {len(opts_monthly):,} bars  "
      f"{len(common_monthly)} days  ({common_monthly[0]} to {common_monthly[-1]})")

iv_window_analysis("NIFTY MONTHLY (Jun30)", fut, opts_monthly, "2026-06-30", common_monthly)
run_window_backtest("NIFTY MONTHLY (Jun30)", fut, opts_monthly, common_monthly, show_detail=True)
full_day_backtest("NIFTY MONTHLY (Jun30)", fut, opts_monthly, common_monthly)

# ──────────────────────────────────────────────────────────────────────────────
# PART 2: WEEKLY (Jun16 + Jun23)
# ──────────────────────────────────────────────────────────────────────────────
opts_jun16 = load_opts(["2026-06-16"])
opts_jun23 = load_opts(["2026-06-23"])

# For weekly, each day we use the NEARER expiry (front-week)
# Jun16 is front-week for its available dates; Jun23 is the next week
# We use Jun16 data for all dates Jun16 is active (until Jun12 = last data)
# and Jun23 data for all dates after Jun16 expires — but we only have Jun12 as last date
# So use Jun16 as the primary weekly for analysis, Jun23 as supplementary

# Determine which days each weekly has data for
days_jun16 = sorted(set(fut["date"]) & set(opts_jun16["date"])) if not opts_jun16.empty else []
days_jun23 = sorted(set(fut["date"]) & set(opts_jun23["date"])) if not opts_jun23.empty else []

print(f"\n  Weekly Jun16: {len(opts_jun16):,} bars  {len(days_jun16)} days  "
      f"({days_jun16[0] if days_jun16 else '?'} to {days_jun16[-1] if days_jun16 else '?'})")
print(f"  Weekly Jun23: {len(opts_jun23):,} bars  {len(days_jun23)} days  "
      f"({days_jun23[0] if days_jun23 else '?'} to {days_jun23[-1] if days_jun23 else '?'})")

# DTE analysis for Jun16
if days_jun16:
    expiry_date_16 = datetime.date(2026, 6, 16)
    print(f"\n  DTE distribution for Jun16 weekly:")
    dte_buckets: dict[str,list] = {"DTE>14":[],"DTE 8-14":[],"DTE 4-7":[],"DTE 1-3":[]}
    for d in days_jun16:
        dte = (expiry_date_16 - datetime.date.fromisoformat(d)).days
        if   dte > 14: dte_buckets["DTE>14"].append(d)
        elif dte >= 8:  dte_buckets["DTE 8-14"].append(d)
        elif dte >= 4:  dte_buckets["DTE 4-7"].append(d)
        else:           dte_buckets["DTE 1-3"].append(d)
    for bkt, days in dte_buckets.items():
        print(f"    {bkt:>10}: {len(days)} days  {days[:3]}{'...' if len(days)>3 else ''}")

# IV analysis for Jun16
if days_jun16:
    iv_window_analysis("NIFTY WEEKLY Jun16", fut, opts_jun16, "2026-06-16", days_jun16)
    run_window_backtest("NIFTY WEEKLY Jun16", fut, opts_jun16, days_jun16, show_detail=True)
    full_day_backtest("NIFTY WEEKLY Jun16", fut, opts_jun16, days_jun16)

# IV analysis for Jun23
if days_jun23:
    iv_window_analysis("NIFTY WEEKLY Jun23", fut, opts_jun23, "2026-06-23", days_jun23)
    run_window_backtest("NIFTY WEEKLY Jun23", fut, opts_jun23, days_jun23, show_detail=False)
    full_day_backtest("NIFTY WEEKLY Jun23", fut, opts_jun23, days_jun23)

print("\nDone.")
