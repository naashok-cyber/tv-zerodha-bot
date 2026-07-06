#!/usr/bin/env python3
"""
Backtest: Intraday short straddle on NATURALGAS JUN26 options.
Entry  : sell ATM CE + PE at close of 09:05 bar
Exit   : close of 23:25 bar (EOD), OR per-leg 50% SL (whichever first)
SL rule: if any bar's HIGH >= 1.5x entry close for that leg
         -> exit that leg at the SL price, exit other leg at bar close
         -> exit BOTH legs immediately
Lot    : 1250 MMBtu
"""
from __future__ import annotations
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from pathlib import Path

DATA     = Path("data/ng_options")
LOT_SIZE = 1250
SL_PCT   = 0.50   # 50% per-leg SL

# ── Load futures ─────────────────────────────────────────────────────────────
fut = pd.read_csv(DATA / "NG_futures_5min.csv")
fut["dt"] = pd.to_datetime(fut["date"] + " " + fut["time"]).dt.tz_localize("Asia/Kolkata")
fut = fut[fut["tradingsymbol"] == "NATURALGAS26JUNFUT"].sort_values("dt").reset_index(drop=True)

# ── Load all JUN26 options ────────────────────────────────────────────────────
dfs = [pd.read_csv(p) for p in sorted((DATA / "2026-06").glob("*_5min.csv"))]
opts = pd.concat(dfs, ignore_index=True)
opts["dt"] = pd.to_datetime(opts["date"] + " " + opts["time"]).dt.tz_localize("Asia/Kolkata")
opts["strike"] = opts["strike"].astype(float)
opts = opts.sort_values("dt").reset_index(drop=True)

# ── Identify last 30 common trading days ─────────────────────────────────────
fut_days  = set(fut["date"].unique())
opts_days = set(opts["date"].unique())
common_days = sorted(fut_days & opts_days)[-30:]

print("=" * 125)
print("  NATURALGAS JUN26 — Intraday Short Straddle Backtest")
print("  Entry: 09:05 IST | Exit: 23:25 IST | SL: 50% per-leg | Lot: 1250")
print("=" * 125)
print(f"  Period: {common_days[0]} to {common_days[-1]}  ({len(common_days)} days)")
print()

# ── Backtest loop ─────────────────────────────────────────────────────────────
results = []

for day in common_days:
    fut_day  = fut[fut["date"] == day].sort_values("dt")
    opts_day = opts[opts["date"] == day].sort_values("dt")

    # ─ Entry: futures price at/after 09:05 ─
    ef = fut_day[fut_day["time"] >= "09:05:00"]
    if ef.empty:
        ef = fut_day
    if ef.empty:
        continue
    F = float(ef.iloc[0]["close"])
    atm = float(round(round(F / 5) * 5))

    # ─ Find nearest available strike ─
    avail = sorted(opts_day["strike"].unique())
    if not avail:
        continue
    atm_used = min(avail, key=lambda s: abs(s - atm))
    atm_diff = abs(atm_used - atm)   # track how far we deviated

    ce_day = opts_day[(opts_day["strike"] == atm_used) & (opts_day["option_type"] == "CE")].sort_values("dt")
    pe_day = opts_day[(opts_day["strike"] == atm_used) & (opts_day["option_type"] == "PE")].sort_values("dt")

    if ce_day.empty or pe_day.empty:
        print(f"  {day}: missing CE or PE for strike {atm_used:.0f} — skip")
        continue

    # ─ Entry prices: close of 09:05 bar (or first bar >= 09:05) ─
    ce_e_bars = ce_day[ce_day["time"] >= "09:05:00"]
    pe_e_bars = pe_day[pe_day["time"] >= "09:05:00"]
    if ce_e_bars.empty: ce_e_bars = ce_day
    if pe_e_bars.empty: pe_e_bars = pe_day

    ce_entry = float(ce_e_bars.iloc[0]["close"])
    pe_entry = float(pe_e_bars.iloc[0]["close"])
    entry_time = ce_e_bars.iloc[0]["time"]

    # Skip if entry price is zero/suspicious (illiquid bar)
    if ce_entry <= 0.1 or pe_entry <= 0.1:
        print(f"  {day}: illiquid entry (CE={ce_entry}, PE={pe_entry}) — skip")
        continue

    ce_sl_price = ce_entry * (1 + SL_PCT)
    pe_sl_price = pe_entry * (1 + SL_PCT)

    # ─ Intraday simulation: all bars AFTER entry bar up to 23:25 ─
    ce_intra = ce_day[(ce_day["time"] > entry_time) & (ce_day["time"] <= "23:25:00")].sort_values("time")
    pe_intra = pe_day[(pe_day["time"] > entry_time) & (pe_day["time"] <= "23:25:00")].sort_values("time")

    # Index by time for merging
    ce_idx = ce_intra.set_index("time")[["high", "close"]].rename(columns={"high": "ce_h", "close": "ce_c"})
    pe_idx = pe_intra.set_index("time")[["high", "close"]].rename(columns={"high": "pe_h", "close": "pe_c"})
    bars = ce_idx.join(pe_idx, how="outer").sort_index().ffill()

    ce_exit = pe_exit = None
    exit_reason = "EOD"
    exit_time_actual = "23:25:00"
    sl_leg = None

    last_ce_c = ce_entry
    last_pe_c = pe_entry

    for bar_time, row in bars.iterrows():
        ce_h = row.get("ce_h", np.nan)
        pe_h = row.get("pe_h", np.nan)
        ce_c = row.get("ce_c", np.nan)
        pe_c = row.get("pe_c", np.nan)

        if not np.isnan(ce_c): last_ce_c = ce_c
        if not np.isnan(pe_c): last_pe_c = pe_c

        ce_hit = (not np.isnan(ce_h)) and (ce_h >= ce_sl_price)
        pe_hit = (not np.isnan(pe_h)) and (pe_h >= pe_sl_price)

        if ce_hit or pe_hit:
            ce_exit = ce_sl_price if ce_hit else last_ce_c
            pe_exit = pe_sl_price if pe_hit else last_pe_c
            exit_reason = "SL"
            exit_time_actual = bar_time
            sl_leg = ("BOTH" if (ce_hit and pe_hit) else
                      "CE"   if ce_hit else "PE")
            break

        if bar_time >= "23:25:00":
            ce_exit = last_ce_c
            pe_exit = last_pe_c
            exit_time_actual = bar_time
            break

    # Fallback: use last known bar
    if ce_exit is None:
        last_ce = ce_intra.iloc[-1]["close"] if not ce_intra.empty else ce_entry
        last_pe = pe_intra.iloc[-1]["close"] if not pe_intra.empty else pe_entry
        ce_exit = float(last_ce)
        pe_exit = float(last_pe)
        exit_reason = "EOD"
        exit_time_actual = ce_intra.iloc[-1]["time"] if not ce_intra.empty else "23:25:00"

    ce_pnl     = ce_entry - ce_exit
    pe_pnl     = pe_entry - pe_exit
    unit_pnl   = ce_pnl + pe_pnl
    lot_pnl    = unit_pnl * LOT_SIZE
    str_entry  = ce_entry + pe_entry
    str_exit   = ce_exit  + pe_exit

    results.append({
        "date":      day,
        "F":         F,
        "atm":       atm_used,
        "atm_diff":  atm_diff,
        "ce_in":     ce_entry,
        "pe_in":     pe_entry,
        "str_in":    str_entry,
        "ce_out":    ce_exit,
        "pe_out":    pe_exit,
        "str_out":   str_exit,
        "ce_pnl":    ce_pnl,
        "pe_pnl":    pe_pnl,
        "unit_pnl":  unit_pnl,
        "lot_pnl":   lot_pnl,
        "exit":      exit_reason + (f"({sl_leg})" if sl_leg else ""),
        "exit_time": str(exit_time_actual),
        "sl_leg":    sl_leg,
    })

# ── Results table ─────────────────────────────────────────────────────────────
hdr = (f"  {'Date':>12} {'F':>6} {'ATM':>5} {'CE_in':>6} {'PE_in':>6} "
       f"{'Strd_in':>8} {'CE_out':>6} {'PE_out':>6} "
       f"{'PnL/u':>7} {'PnL/lot':>9} {'Exit':<20} {'Result'}")
print(hdr)
print("-" * 125)

cumulative = 0.0
wins = losses = sl_count = 0
ce_sl_count = pe_sl_count = 0

for r in results:
    cumulative += r["lot_pnl"]
    w = "WIN " if r["unit_pnl"] > 0 else "LOSS"
    wins   += 1 if r["unit_pnl"] > 0 else 0
    losses += 1 if r["unit_pnl"] <= 0 else 0
    if "SL" in r["exit"]:
        sl_count += 1
        if r["sl_leg"] in ("CE", "BOTH"): ce_sl_count += 1
        if r["sl_leg"] in ("PE", "BOTH"): pe_sl_count += 1

    flag = "*" if r["atm_diff"] >= 2.5 else " "  # mark days where ATM deviated

    print(f"  {r['date']}  {r['F']:>6.1f}  {r['atm']:>5.0f}{flag} "
          f"{r['ce_in']:>6.2f}  {r['pe_in']:>6.2f}  "
          f"{r['str_in']:>8.2f}  "
          f"{r['ce_out']:>6.2f}  {r['pe_out']:>6.2f}  "
          f"{r['unit_pnl']:>+7.2f}  "
          f"{r['lot_pnl']:>+9.0f}  "
          f"{r['exit']:<20} {w}  cumul: {cumulative:+,.0f}")

n = len(results)
print("-" * 125)
print()
print("  SUMMARY")
print("  " + "-" * 50)
print(f"  Days traded     : {n}")
print(f"  Win / Loss      : {wins} / {losses}  ({wins/n*100:.0f}% win rate)")
print(f"  SL triggers     : {sl_count}  (CE leg: {ce_sl_count}, PE leg: {pe_sl_count})")
print(f"  Avg straddle in : Rs {sum(r['str_in'] for r in results)/n:.2f}")
print(f"  Avg straddle out: Rs {sum(r['str_out'] for r in results)/n:.2f}")
print()
print(f"  Gross P&L       : Rs {cumulative:+,.0f} / lot")
print(f"  Avg per day     : Rs {cumulative/n:+,.0f} / lot")
print()
print("  Slippage impact (estimate):")
for slip in [0.20, 0.40, 0.60, 1.00]:
    net = cumulative - slip * LOT_SIZE * n
    print(f"    Rs {slip:.2f}/unit/day slippage -> Net P&L Rs {net:+,.0f}  ({net/n:+.0f}/day)")
print()
print("  * = ATM deviated (nearest available strike used)")
print("  [SL: exit both legs when any leg HIGH >= 1.5x entry; losing leg exits at SL price]")

# ── Day-of-week breakdown ─────────────────────────────────────────────────────
print()
print("  DoW Breakdown:")
import datetime
dow_map = {0:"Mon", 1:"Tue", 2:"Wed", 3:"Thu", 4:"Fri"}
dow_stats: dict[str, list] = {v: [] for v in dow_map.values()}
for r in results:
    d = datetime.date.fromisoformat(r["date"])
    dow = dow_map.get(d.weekday(), "?")
    dow_stats[dow].append(r["lot_pnl"])

print(f"  {'DoW':>5}  {'Days':>5}  {'Wins':>5}  {'SL':>5}  {'Avg P&L/lot':>13}  {'Total':>10}")
for dow in ["Mon","Tue","Wed","Thu","Fri"]:
    vals = dow_stats[dow]
    if not vals: continue
    w = sum(1 for v in vals if v > 0)
    total = sum(vals)
    avg   = total / len(vals)
    # count SL days for this DoW
    sl_d = sum(1 for r in results
               if "SL" in r["exit"] and
               dow_map.get(datetime.date.fromisoformat(r["date"]).weekday(),"") == dow)
    print(f"  {dow:>5}  {len(vals):>5}  {w:>5}  {sl_d:>5}  {avg:>+13,.0f}  {total:>+10,.0f}")
