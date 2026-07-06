#!/usr/bin/env python3
"""
BANKNIFTY JUN26 monthly options — IV patterns + windowed straddle backtest.
Session: 09:15–15:30 IST  |  Lot: 30  |  SL: 50% per-leg
Windows (1.5 hr each):
  A  09:15–10:45
  B  10:45–12:15
  C  12:15–13:45
  D  13:45–15:30
Underlying model: BSM (equity index with dividend yield q=0)
"""
from __future__ import annotations
import io, sys, math, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import datetime
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.optimize import brentq
from scipy.stats import norm

DATA     = Path("data/banknifty_options")
LOT_SIZE = 30
SL_PCT   = 0.50   # 50% per-leg SL
r        = 0.065  # risk-free rate ~6.5% (India)
q        = 0.0    # dividend yield (index — dividends embedded in futures)

# ── BSM pricing & IV ─────────────────────────────────────────────────────────
def bsm_price(F, K, T, r, q, sigma, flag):
    """Black-Scholes-Merton using futures price (forward = F*e^(-q*T) ~ F for index)."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(F / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if flag.upper() == "CE":
        return F * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - F * math.exp(-q * T) * norm.cdf(-d1)

def solve_iv(F, K, T, r, q, market_price, flag):
    if T <= 1e-6 or market_price <= 0 or F <= 0:
        return np.nan
    intrinsic = max(0, (F - K) if flag == "CE" else (K - F))
    if market_price <= intrinsic * 1.001:
        return np.nan
    try:
        iv = brentq(lambda s: bsm_price(F, K, T, r, q, s, flag) - market_price,
                    0.01, 5.0, xtol=1e-4, maxiter=50)
        return iv
    except (ValueError, RuntimeError):
        return np.nan

# ── Load data ────────────────────────────────────────────────────────────────
print("=" * 100)
print("  BANKNIFTY JUN26 (expiry 2026-06-30) — IV Analysis + Windowed Short Straddle")
print("=" * 100)

fut = pd.read_csv(DATA / "futures_5min.csv")
fut["dt"] = pd.to_datetime(fut["date"] + " " + fut["time"]).dt.tz_localize("Asia/Kolkata")
fut["close"] = fut["close"].astype(float)
front_sym = "BANKNIFTY26JUNFUT"
fut = fut[fut["tradingsymbol"] == front_sym].sort_values("dt").reset_index(drop=True)
print(f"\n  Futures loaded: {len(fut):,} bars  ({fut['date'].nunique()} days)")

# JUN26 options
opt_dir = DATA / "2026-06-30"
dfs = [pd.read_csv(p) for p in sorted(opt_dir.glob("*_5min.csv"))]
opts = pd.concat(dfs, ignore_index=True)
opts["dt"] = pd.to_datetime(opts["date"] + " " + opts["time"]).dt.tz_localize("Asia/Kolkata")
opts["strike"] = opts["strike"].astype(float)
opts = opts.sort_values("dt").reset_index(drop=True)
print(f"  Options loaded: {len(opts):,} bars across {opts['option_type'].value_counts().to_dict()}")

EXPIRY_DT = datetime.datetime(2026, 6, 30, 15, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))

common_days = sorted(set(fut["date"].unique()) & set(opts["date"].unique()))
print(f"  Common trading days: {len(common_days)}  ({common_days[0]} to {common_days[-1]})")

# ── Section 1: IV by window ───────────────────────────────────────────────────
WINDOWS = [
    ("09:15-10:45", "09:15:00", "10:45:00"),
    ("10:45-12:15", "10:45:00", "12:15:00"),
    ("12:15-13:45", "12:15:00", "13:45:00"),
    ("13:45-15:30", "13:45:00", "15:30:00"),
]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

print("\n" + "=" * 100)
print("  SECTION 1: INTRADAY IV PATTERNS BY WINDOW")
print("=" * 100)

iv_records = []

for day in common_days:
    fut_day  = fut[fut["date"] == day].sort_values("time")
    opts_day = opts[opts["date"] == day].sort_values("time")
    dow_str  = DOW[datetime.date.fromisoformat(day).weekday()]

    # Expiry in calendar days
    exp_date = datetime.date(2026, 6, 30)
    trading_date = datetime.date.fromisoformat(day)
    T_days = (exp_date - trading_date).days
    T_yrs  = max(T_days / 365.0, 1 / 365)

    for win_label, win_start, win_end in WINDOWS:
        win_fut = fut_day[(fut_day["time"] >= win_start) & (fut_day["time"] <= win_end)]
        if win_fut.empty:
            continue
        F_bar = win_fut["close"].astype(float).mean()

        # ATM at window mid
        avail_strikes = sorted(opts_day["strike"].unique())
        if not avail_strikes:
            continue
        atm = min(avail_strikes, key=lambda s: abs(s - F_bar))

        ce = opts_day[(opts_day["strike"] == atm) & (opts_day["option_type"] == "CE")]
        pe = opts_day[(opts_day["strike"] == atm) & (opts_day["option_type"] == "PE")]

        win_ce = ce[(ce["time"] >= win_start) & (ce["time"] <= win_end)]
        win_pe = pe[(pe["time"] >= win_start) & (pe["time"] <= win_end)]

        if win_ce.empty or win_pe.empty:
            continue

        # mid-window bar for IV
        mid_ce = win_ce.iloc[len(win_ce)//2]
        mid_pe = win_pe.iloc[len(win_pe)//2]
        F_mid  = float(fut_day[(fut_day["time"] >= win_start) & (fut_day["time"] <= win_end)]["close"].mean())

        iv_ce = solve_iv(F_mid, atm, T_yrs, r, q, float(mid_ce["close"]), "CE")
        iv_pe = solve_iv(F_mid, atm, T_yrs, r, q, float(mid_pe["close"]), "PE")
        iv_atm = np.nanmean([iv_ce, iv_pe]) if not (np.isnan(iv_ce) and np.isnan(iv_pe)) else np.nan

        # IV at window start vs window end (for contraction measure)
        first_ce = win_ce.iloc[0]
        last_ce  = win_ce.iloc[-1]
        first_pe = win_pe.iloc[0]
        last_pe  = win_pe.iloc[-1]
        F_start = float(win_fut.iloc[0]["close"]) if not win_fut.empty else F_mid
        F_end   = float(win_fut.iloc[-1]["close"]) if not win_fut.empty else F_mid

        iv_start = np.nanmean([
            solve_iv(F_start, atm, T_yrs, r, q, float(first_ce["close"]), "CE"),
            solve_iv(F_start, atm, T_yrs, r, q, float(first_pe["close"]), "PE"),
        ])
        iv_end = np.nanmean([
            solve_iv(F_end, atm, T_yrs, r, q, float(last_ce["close"]), "CE"),
            solve_iv(F_end, atm, T_yrs, r, q, float(last_pe["close"]), "PE"),
        ])
        iv_delta = iv_end - iv_start  # negative = contraction (good for short straddle)

        # Straddle value: start vs end
        str_start = float(first_ce["close"]) + float(first_pe["close"])
        str_end   = float(last_ce["close"])  + float(last_pe["close"])
        str_pct   = (str_end - str_start) / str_start * 100 if str_start > 0 else np.nan

        iv_records.append({
            "date": day, "dow": dow_str, "window": win_label,
            "F": F_bar, "atm": atm,
            "str_start": str_start, "str_end": str_end, "str_pct": str_pct,
            "iv_atm": iv_atm * 100 if not np.isnan(iv_atm) else np.nan,
            "iv_start": iv_start * 100 if not np.isnan(iv_start) else np.nan,
            "iv_end":   iv_end   * 100 if not np.isnan(iv_end) else np.nan,
            "iv_delta": iv_delta * 100 if not np.isnan(iv_delta) else np.nan,
        })

iv_df = pd.DataFrame(iv_records)

print(f"\n  {'Window':>15}  {'Days':>5}  {'Avg IV%':>8}  {'Avg IV start':>12}  "
      f"{'Avg IV end':>10}  {'IV Δ (pp)':>10}  {'Strd start':>10}  {'Strd end':>9}  {'Strd Δ%':>8}  Direction")
print("  " + "-" * 120)

for win_label, _, _ in WINDOWS:
    sub = iv_df[iv_df["window"] == win_label].dropna(subset=["iv_delta","str_pct"])
    if sub.empty:
        continue
    n        = len(sub)
    avg_iv   = sub["iv_atm"].mean()
    avg_is   = sub["iv_start"].mean()
    avg_ie   = sub["iv_end"].mean()
    avg_idel = sub["iv_delta"].mean()
    avg_ss   = sub["str_start"].mean()
    avg_se   = sub["str_end"].mean()
    avg_sp   = sub["str_pct"].mean()
    direction = "CONTRACT" if avg_idel < -0.5 else ("EXPAND" if avg_idel > 0.5 else "flat")
    print(f"  {win_label:>15}  {n:>5}  {avg_iv:>8.1f}%  {avg_is:>12.1f}%  "
          f"{avg_ie:>10.1f}%  {avg_idel:>+10.2f}pp  {avg_ss:>10.1f}  {avg_se:>9.1f}  "
          f"{avg_sp:>+8.1f}%  {direction}")

# ── DoW breakdown by window ───────────────────────────────────────────────────
print("\n  DoW × Window Straddle Change%:")
print(f"  {'Window':>15}", end="")
for d in ["Mon","Tue","Wed","Thu","Fri"]:
    print(f"  {d:>8}", end="")
print()
print("  " + "-" * 60)
for win_label, _, _ in WINDOWS:
    sub = iv_df[iv_df["window"] == win_label]
    print(f"  {win_label:>15}", end="")
    for d in ["Mon","Tue","Wed","Thu","Fri"]:
        dsub = sub[sub["dow"] == d]["str_pct"].dropna()
        val  = f"{dsub.mean():>+8.1f}%" if not dsub.empty else f"{'—':>9}"
        print(f"  {val}", end="")
    print()

# ── Section 2: Windowed Short Straddle Backtest ───────────────────────────────
print("\n" + "=" * 100)
print("  SECTION 2: WINDOWED SHORT STRADDLE BACKTEST  (SL=50% per-leg, Lot=30)")
print("=" * 100)

def trade_window(day, win_start, win_end, fut_day, opts_day):
    """Simulate one straddle window. Returns result dict or None."""
    # ATM at window start from futures
    ef = fut_day[fut_day["time"] >= win_start]
    if ef.empty: return None
    F = float(ef.iloc[0]["close"])
    avail = sorted(opts_day["strike"].unique())
    if not avail: return None
    atm_used = min(avail, key=lambda s: abs(s - F))

    ce_all = opts_day[(opts_day["strike"] == atm_used) & (opts_day["option_type"] == "CE")].sort_values("time")
    pe_all = opts_day[(opts_day["strike"] == atm_used) & (opts_day["option_type"] == "PE")].sort_values("time")
    if ce_all.empty or pe_all.empty: return None

    ce_e = ce_all[ce_all["time"] >= win_start]
    pe_e = pe_all[pe_all["time"] >= win_start]
    if ce_e.empty or pe_e.empty: return None

    ce_entry   = float(ce_e.iloc[0]["close"])
    pe_entry   = float(pe_e.iloc[0]["close"])
    entry_time = ce_e.iloc[0]["time"]

    if ce_entry <= 0.5 or pe_entry <= 0.5: return None

    ce_sl = ce_entry * (1 + SL_PCT)
    pe_sl = pe_entry * (1 + SL_PCT)

    ce_intra = ce_all[(ce_all["time"] > entry_time) & (ce_all["time"] <= win_end)].sort_values("time")
    pe_intra = pe_all[(pe_all["time"] > entry_time) & (pe_all["time"] <= win_end)].sort_values("time")

    ce_idx = ce_intra.set_index("time")[["high","close"]].rename(columns={"high":"ce_h","close":"ce_c"})
    pe_idx = pe_intra.set_index("time")[["high","close"]].rename(columns={"high":"pe_h","close":"pe_c"})
    bars   = ce_idx.join(pe_idx, how="outer").sort_index().ffill()

    last_ce = ce_entry; last_pe = pe_entry
    ce_exit = pe_exit = None
    exit_reason = "EOW"; sl_leg = None

    for bar_time, row in bars.iterrows():
        ce_h = row.get("ce_h", np.nan); pe_h = row.get("pe_h", np.nan)
        ce_c = row.get("ce_c", np.nan); pe_c = row.get("pe_c", np.nan)
        if not np.isnan(ce_c): last_ce = ce_c
        if not np.isnan(pe_c): last_pe = pe_c

        ce_hit = (not np.isnan(ce_h)) and ce_h >= ce_sl
        pe_hit = (not np.isnan(pe_h)) and pe_h >= pe_sl
        if ce_hit or pe_hit:
            ce_exit = ce_sl if ce_hit else last_ce
            pe_exit = pe_sl if pe_hit else last_pe
            exit_reason = "SL"
            sl_leg = "BOTH" if (ce_hit and pe_hit) else ("CE" if ce_hit else "PE")
            break
        if bar_time >= win_end:
            ce_exit = last_ce; pe_exit = last_pe; break

    if ce_exit is None:
        ce_exit = float(ce_all[ce_all["time"] <= win_end].iloc[-1]["close"]) if not ce_all[ce_all["time"] <= win_end].empty else ce_entry
        pe_exit = float(pe_all[pe_all["time"] <= win_end].iloc[-1]["close"]) if not pe_all[pe_all["time"] <= win_end].empty else pe_entry

    unit_pnl = (ce_entry - ce_exit) + (pe_entry - pe_exit)
    lot_pnl  = unit_pnl * LOT_SIZE

    return {
        "day": day, "dow": DOW[datetime.date.fromisoformat(day).weekday()],
        "F": F, "atm": atm_used,
        "ce_in": ce_entry, "pe_in": pe_entry, "str_in": ce_entry + pe_entry,
        "ce_out": ce_exit, "pe_out": pe_exit,
        "unit_pnl": unit_pnl, "lot_pnl": lot_pnl,
        "exit": exit_reason + (f"({sl_leg})" if sl_leg else ""),
        "sl_leg": sl_leg,
    }

all_results = {w[0]: [] for w in WINDOWS}

for day in common_days:
    fut_day  = fut[fut["date"] == day].sort_values("time")
    opts_day = opts[opts["date"] == day].sort_values("time")
    for win_label, win_start, win_end in WINDOWS:
        r_trade = trade_window(day, win_start, win_end, fut_day, opts_day)
        if r_trade:
            r_trade["window"] = win_label
            all_results[win_label].append(r_trade)

print()
print(f"  {'Window':>15}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  "
      f"{'WinRate':>8}  {'Avg Strd in':>11}  {'Avg PnL/lot':>12}  {'Total PnL':>12}  Direction")
print("  " + "-" * 110)

window_summary = []
for win_label, _, _ in WINDOWS:
    rows = all_results[win_label]
    if not rows: continue
    n     = len(rows)
    wins  = sum(1 for r in rows if r["unit_pnl"] > 0)
    sls   = sum(1 for r in rows if "SL" in r["exit"])
    total = sum(r["lot_pnl"] for r in rows)
    avg   = total / n
    avg_s = sum(r["str_in"] for r in rows) / n
    wr    = wins / n * 100
    direction = "CONTRACTION" if total > 0 else "EXPANSION"
    print(f"  {win_label:>15}  {n:>5}  {wins:>5}  {sls:>4}  "
          f"{wr:>7.0f}%  {avg_s:>11.1f}  {avg:>+12,.0f}  {total:>+12,.0f}  {direction}")
    window_summary.append((win_label, n, wins, sls, total, avg, avg_s))

# ── Day-by-day detail per window ─────────────────────────────────────────────
print()
for win_label, _, _ in WINDOWS:
    rows = all_results[win_label]
    if not rows: continue
    print(f"  {'─'*110}")
    print(f"  Window: {win_label}")
    print(f"  {'─'*110}")
    print(f"  {'Date':>12}  {'DoW':>4}  {'ATM':>6}  {'CE_in':>7}  {'PE_in':>7}  {'Strd_in':>8}  "
          f"{'CE_out':>7}  {'PE_out':>7}  {'PnL/u':>8}  {'PnL/lot':>9}  {'Exit':<16}")
    print(f"  {'-'*100}")
    running = 0
    for r in rows:
        running += r["lot_pnl"]
        w = "WIN " if r["unit_pnl"] > 0 else "LOSS"
        print(f"  {r['day']}  {r['dow']:>4}  {r['atm']:>6.0f}  "
              f"{r['ce_in']:>7.1f}  {r['pe_in']:>7.1f}  {r['str_in']:>8.1f}  "
              f"{r['ce_out']:>7.1f}  {r['pe_out']:>7.1f}  "
              f"{r['unit_pnl']:>+8.1f}  {r['lot_pnl']:>+9.0f}  "
              f"{r['exit']:<16}  {w}  cum:{running:+,}")
    print()

# ── DoW breakdown per window ──────────────────────────────────────────────────
print("=" * 100)
print("  SECTION 3: DoW × WINDOW BREAKDOWN")
print("=" * 100)
print(f"\n  {'Window':>15}  {'DoW':>4}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  "
      f"{'WinRate':>8}  {'Avg PnL/lot':>12}  {'Total':>12}")
print("  " + "-" * 80)
for win_label, _, _ in WINDOWS:
    rows = all_results[win_label]
    if not rows: continue
    dow_g: dict[str, list] = {}
    for r in rows: dow_g.setdefault(r["dow"], []).append(r)
    for d in ["Mon","Tue","Wed","Thu","Fri"]:
        drows = dow_g.get(d, [])
        if not drows: continue
        dn = len(drows)
        dw = sum(1 for r in drows if r["unit_pnl"] > 0)
        ds = sum(1 for r in drows if "SL" in r["exit"])
        dt = sum(r["lot_pnl"] for r in drows)
        print(f"  {win_label:>15}  {d:>4}  {dn:>5}  {dw:>5}  {ds:>4}  "
              f"{dw/dn*100:>7.0f}%  {dt/dn:>+12,.0f}  {dt:>+12,.0f}")

# ── Section 4: Best window deep-dive ─────────────────────────────────────────
print("\n" + "=" * 100)
print("  SECTION 4: FULL-DAY SHORT STRADDLE (09:15 entry → 15:30 exit)")
print("=" * 100)

eod_results = []
for day in common_days:
    fut_day  = fut[fut["date"] == day].sort_values("time")
    opts_day = opts[opts["date"] == day].sort_values("time")
    r_trade  = trade_window(day, "09:15:00", "15:30:00", fut_day, opts_day)
    if r_trade:
        eod_results.append(r_trade)

print(f"\n  {'Date':>12}  {'DoW':>4}  {'ATM':>6}  {'CE_in':>7}  {'PE_in':>7}  {'Strd_in':>8}  "
      f"{'CE_out':>7}  {'PE_out':>7}  {'PnL/u':>8}  {'PnL/lot':>9}  {'Exit':<16}")
print("  " + "-" * 110)
cumul = 0
for r in eod_results:
    cumul += r["lot_pnl"]
    w = "WIN " if r["unit_pnl"] > 0 else "LOSS"
    print(f"  {r['day']}  {r['dow']:>4}  {r['atm']:>6.0f}  "
          f"{r['ce_in']:>7.1f}  {r['pe_in']:>7.1f}  {r['str_in']:>8.1f}  "
          f"{r['ce_out']:>7.1f}  {r['pe_out']:>7.1f}  "
          f"{r['unit_pnl']:>+8.1f}  {r['lot_pnl']:>+9.0f}  "
          f"{r['exit']:<16}  {w}  cum:{cumul:+,}")

n = len(eod_results)
wins = sum(1 for r in eod_results if r["unit_pnl"] > 0)
sls  = sum(1 for r in eod_results if "SL" in r["exit"])
print(f"\n  Days: {n}  |  Wins: {wins} ({wins/n*100:.0f}%)  |  SL: {sls}")
print(f"  Avg straddle in : {sum(r['str_in'] for r in eod_results)/n:.1f}")
print(f"  Gross P&L       : Rs {cumul:+,} / lot")
print(f"  Avg per day     : Rs {cumul/n:+,.0f} / lot")
print(f"\n  Slippage impact (2-way = entry + exit, ₹ per unit):")
for slip in [5, 10, 15, 20]:
    net = cumul - slip * 2 * n  # 2 legs each way
    print(f"    Rs {slip}/unit/leg -> Net Rs {net:+,}  ({net/n:+.0f}/day)")

# ── DoW for full-day ──────────────────────────────────────────────────────────
print("\n  DoW Breakdown (full day):")
print(f"  {'DoW':>5}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  {'Avg Strd':>9}  {'Avg PnL':>10}  {'Total':>10}")
print(f"  {'-'*60}")
dow_g2: dict[str, list] = {}
for r in eod_results: dow_g2.setdefault(r["dow"], []).append(r)
for d in ["Mon","Tue","Wed","Thu","Fri"]:
    drows = dow_g2.get(d, [])
    if not drows: continue
    dn = len(drows); dw = sum(1 for r in drows if r["unit_pnl"] > 0)
    ds = sum(1 for r in drows if "SL" in r["exit"]); dt = sum(r["lot_pnl"] for r in drows)
    avgs = sum(r["str_in"] for r in drows) / dn
    print(f"  {d:>5}  {dn:>5}  {dw:>5}  {ds:>4}  {avgs:>9.1f}  {dt/dn:>+10,.0f}  {dt:>+10,}")

print("\nDone.")
