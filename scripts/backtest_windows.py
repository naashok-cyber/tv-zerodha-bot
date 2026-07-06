#!/usr/bin/env python3
"""
Windowed short straddle backtest on NATURALGAS JUN26 options.
Each 2.5-hr contraction window is treated as an independent trade:
  - Enter: first 5-min bar at/after window start
  - Exit:  last 5-min bar at/before window end (or per-leg 50% SL)
  - SL:    if any bar HIGH >= 1.5x entry close for that leg -> exit both
ATM is determined fresh at the start of each window using futures price.
Lot: 1250 MMBtu
"""
from __future__ import annotations
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import warnings
warnings.filterwarnings("ignore")

import datetime
import pandas as pd
import numpy as np
from pathlib import Path

DATA     = Path("data/ng_options")
LOT_SIZE = 1250
SL_PCT   = 0.50

WINDOWS = [
    ("09:00-11:30", "09:00:00", "11:30:00"),
    ("11:30-14:00", "11:30:00", "14:00:00"),
    ("14:00-16:30", "14:00:00", "16:30:00"),
    ("16:30-19:00", "16:30:00", "19:00:00"),
    ("19:00-21:30", "19:00:00", "21:30:00"),
    ("21:30-23:30", "21:30:00", "23:30:00"),
]

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ── Load data ────────────────────────────────────────────────────────────────
fut = pd.read_csv(DATA / "NG_futures_5min.csv")
fut["dt"] = pd.to_datetime(fut["date"] + " " + fut["time"]).dt.tz_localize("Asia/Kolkata")
fut = fut[fut["tradingsymbol"] == "NATURALGAS26JUNFUT"].sort_values("dt").reset_index(drop=True)

dfs = [pd.read_csv(p) for p in sorted((DATA / "2026-06").glob("*_5min.csv"))]
opts = pd.concat(dfs, ignore_index=True)
opts["dt"] = pd.to_datetime(opts["date"] + " " + opts["time"]).dt.tz_localize("Asia/Kolkata")
opts["strike"] = opts["strike"].astype(float)
opts = opts.sort_values("dt").reset_index(drop=True)

common_days = sorted(set(fut["date"].unique()) & set(opts["date"].unique()))[-30:]

# ── Helper: simulate one window trade ────────────────────────────────────────
def trade_window(day: str, win_start: str, win_end: str,
                 fut_day: pd.DataFrame, opts_day: pd.DataFrame) -> dict | None:

    # ATM at window start
    ef = fut_day[fut_day["time"] >= win_start]
    if ef.empty:
        return None
    F = float(ef.iloc[0]["close"])
    atm = float(round(round(F / 5) * 5))

    avail = sorted(opts_day["strike"].unique())
    if not avail:
        return None
    atm_used = min(avail, key=lambda s: abs(s - atm))

    ce_all = opts_day[(opts_day["strike"] == atm_used) & (opts_day["option_type"] == "CE")].sort_values("time")
    pe_all = opts_day[(opts_day["strike"] == atm_used) & (opts_day["option_type"] == "PE")].sort_values("time")
    if ce_all.empty or pe_all.empty:
        return None

    ce_e = ce_all[ce_all["time"] >= win_start]
    pe_e = pe_all[pe_all["time"] >= win_start]
    if ce_e.empty or pe_e.empty:
        return None

    ce_entry = float(ce_e.iloc[0]["close"])
    pe_entry = float(pe_e.iloc[0]["close"])
    entry_time = ce_e.iloc[0]["time"]

    if ce_entry <= 0.1 or pe_entry <= 0.1:
        return None

    ce_sl = ce_entry * (1 + SL_PCT)
    pe_sl = pe_entry * (1 + SL_PCT)

    # Intraday bars from after entry bar up to win_end
    ce_intra = ce_all[(ce_all["time"] > entry_time) & (ce_all["time"] <= win_end)].sort_values("time")
    pe_intra = pe_all[(pe_all["time"] > entry_time) & (pe_all["time"] <= win_end)].sort_values("time")

    ce_idx = ce_intra.set_index("time")[["high","close"]].rename(columns={"high":"ce_h","close":"ce_c"})
    pe_idx = pe_intra.set_index("time")[["high","close"]].rename(columns={"high":"pe_h","close":"pe_c"})
    bars = ce_idx.join(pe_idx, how="outer").sort_index().ffill()

    last_ce = ce_entry
    last_pe = pe_entry
    ce_exit = pe_exit = None
    exit_reason = "EOW"   # end of window
    sl_leg = None

    for bar_time, row in bars.iterrows():
        ce_h = row.get("ce_h", np.nan)
        pe_h = row.get("pe_h", np.nan)
        ce_c = row.get("ce_c", np.nan)
        pe_c = row.get("pe_c", np.nan)

        if not np.isnan(ce_c): last_ce = ce_c
        if not np.isnan(pe_c): last_pe = pe_c

        ce_hit = (not np.isnan(ce_h)) and ce_h >= ce_sl
        pe_hit = (not np.isnan(pe_h)) and pe_h >= pe_sl

        if ce_hit or pe_hit:
            ce_exit = ce_sl if ce_hit else last_ce
            pe_exit = pe_sl if pe_hit else last_pe
            exit_reason = "SL"
            sl_leg = ("BOTH" if (ce_hit and pe_hit) else
                      "CE"   if ce_hit else "PE")
            break

        if bar_time >= win_end:
            ce_exit = last_ce
            pe_exit = last_pe
            break

    if ce_exit is None:
        # use last available bar before window end
        last_ce_b = ce_all[ce_all["time"] <= win_end]
        last_pe_b = pe_all[pe_all["time"] <= win_end]
        ce_exit = float(last_ce_b.iloc[-1]["close"]) if not last_ce_b.empty else ce_entry
        pe_exit = float(last_pe_b.iloc[-1]["close"]) if not last_pe_b.empty else pe_entry

    ce_pnl   = ce_entry - ce_exit
    pe_pnl   = pe_entry - pe_exit
    unit_pnl = ce_pnl + pe_pnl
    lot_pnl  = unit_pnl * LOT_SIZE

    return {
        "day":       day,
        "dow":       DOW[datetime.date.fromisoformat(day).weekday()],
        "F":         F,
        "atm":       atm_used,
        "ce_in":     ce_entry,
        "pe_in":     pe_entry,
        "ce_out":    ce_exit,
        "pe_out":    pe_exit,
        "unit_pnl":  unit_pnl,
        "lot_pnl":   lot_pnl,
        "exit":      exit_reason + (f"({sl_leg})" if sl_leg else ""),
        "sl_leg":    sl_leg,
    }

# ── Run all windows across all days ──────────────────────────────────────────
all_results: dict[str, list] = {w[0]: [] for w in WINDOWS}

for day in common_days:
    fut_day  = fut[fut["date"] == day].sort_values("time")
    opts_day = opts[opts["date"] == day].sort_values("time")

    for win_label, win_start, win_end in WINDOWS:
        r = trade_window(day, win_start, win_end, fut_day, opts_day)
        if r:
            r["window"] = win_label
            all_results[win_label].append(r)

# ── Print per-window summary table ───────────────────────────────────────────
print("=" * 100)
print("  NATURALGAS JUN26 — Windowed Short Straddle Backtest")
print("  Each 2.5-hr window = independent trade | SL: 50% per-leg | Lot: 1250")
print("=" * 100)

print()
print(f"  {'Window':>15}  {'Days':>5}  {'Wins':>5}  {'SL':>4}  "
      f"{'WinRate':>8}  {'Avg PnL/lot':>12}  {'Total PnL':>12}  Direction")
print("  " + "-" * 90)

window_totals = []
for win_label, _, _ in WINDOWS:
    rows = all_results[win_label]
    if not rows:
        continue
    n     = len(rows)
    wins  = sum(1 for r in rows if r["unit_pnl"] > 0)
    sls   = sum(1 for r in rows if "SL" in r["exit"])
    total = sum(r["lot_pnl"] for r in rows)
    avg   = total / n
    wr    = wins / n * 100
    tag   = "CONTRACTION" if win_label != "09:00-11:30" else "expansion"
    print(f"  {win_label:>15}  {n:>5}  {wins:>5}  {sls:>4}  "
          f"{wr:>7.0f}%  {avg:>+12,.0f}  {total:>+12,.0f}  {tag}")
    window_totals.append((win_label, n, wins, sls, total, avg))

print()

# ── Detailed day-by-day per window ───────────────────────────────────────────
CONTRACTION_WINDOWS = [w[0] for w in WINDOWS if w[0] != "09:00-11:30"]

for win_label in CONTRACTION_WINDOWS:
    rows = all_results[win_label]
    if not rows:
        continue
    print(f"  {'─'*100}")
    print(f"  Window: {win_label}")
    print(f"  {'─'*100}")
    print(f"  {'Date':>12}  {'DoW':>4}  {'ATM':>5}  {'CE_in':>6}  {'PE_in':>6}  "
          f"{'CE_out':>6}  {'PE_out':>6}  {'PnL/u':>7}  {'PnL/lot':>9}  {'Exit':<14}")
    print(f"  {'-'*95}")

    running = 0
    for r in rows:
        running += r["lot_pnl"]
        w = "WIN " if r["unit_pnl"] > 0 else "LOSS"
        print(f"  {r['day']}  {r['dow']:>4}  {r['atm']:>5.0f}  "
              f"{r['ce_in']:>6.2f}  {r['pe_in']:>6.2f}  "
              f"{r['ce_out']:>6.2f}  {r['pe_out']:>6.2f}  "
              f"{r['unit_pnl']:>+7.2f}  {r['lot_pnl']:>+9.0f}  "
              f"{r['exit']:<14}  {w}  cum:{running:+,}")
    print()

# ── Combined: best contraction windows only (19:00-21:30 + 16:30-19:00) ─────
print("=" * 100)
print("  COMBINED: Trade ONLY 16:30-19:00 + 19:00-21:30  (two strongest contraction windows)")
print("=" * 100)
combo_rows = all_results["16:30-19:00"] + all_results["19:00-21:30"]
combo_rows_by_day: dict[str, list] = {}
for r in combo_rows:
    combo_rows_by_day.setdefault(r["day"], []).append(r)

combo_total = 0
combo_wins = combo_losses = combo_sl = 0
print(f"\n  {'Date':>12}  {'DoW':>4}  {'Window':>15}  {'PnL/lot':>9}  {'Exit':<14}")
print(f"  {'-'*75}")
for day in sorted(combo_rows_by_day):
    for r in combo_rows_by_day[day]:
        combo_total += r["lot_pnl"]
        w = "WIN " if r["unit_pnl"] > 0 else "LOSS"
        combo_wins   += 1 if r["unit_pnl"] > 0 else 0
        combo_losses += 1 if r["unit_pnl"] <= 0 else 0
        combo_sl     += 1 if "SL" in r["exit"] else 0
        print(f"  {r['day']}  {r['dow']:>4}  {r['window']:>15}  "
              f"{r['lot_pnl']:>+9.0f}  {r['exit']:<14}  {w}  cum:{combo_total:+,}")

n_trades = len(combo_rows)
print(f"\n  Trades: {n_trades}  |  Wins: {combo_wins} ({combo_wins/n_trades*100:.0f}%)  |  "
      f"SL: {combo_sl}  |  Gross: Rs {combo_total:+,}/lot  |  Avg: Rs {combo_total/n_trades:+,.0f}/trade")
print()
print("  Slippage impact:")
for slip in [0.20, 0.40, 0.60]:
    net = combo_total - slip * LOT_SIZE * n_trades
    print(f"    Rs {slip:.2f}/unit/trade -> Net Rs {net:+,}  ({net/n_trades:+,.0f}/trade)")

# ── DoW breakdown for 19:00-21:30 (best window) ──────────────────────────────
print()
print("  DoW breakdown — 19:00-21:30 only:")
best = all_results["19:00-21:30"]
dow_g: dict[str, list] = {}
for r in best:
    dow_g.setdefault(r["dow"], []).append(r["lot_pnl"])
print(f"  {'DoW':>5}  {'Days':>5}  {'Wins':>5}  {'Avg/lot':>10}  {'Total':>10}")
for d in ["Mon","Tue","Wed","Thu","Fri"]:
    vals = dow_g.get(d, [])
    if not vals: continue
    w = sum(1 for v in vals if v > 0)
    print(f"  {d:>5}  {len(vals):>5}  {w:>5}  {sum(vals)/len(vals):>+10,.0f}  {sum(vals):>+10,}")
