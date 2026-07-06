"""
Deep straddle analysis:
  1. Per-expiry best entry slots (month-by-month)
  2. Cycle phase analysis (which week within the 30-day window works best)
  3. Premium contraction/expansion by days-to-expiry
  4. Day-of-week × cycle-phase heatmap

Usage:
    python scripts/analyze_straddle_deep.py
    python scripts/analyze_straddle_deep.py --underlying NIFTY
    python scripts/analyze_straddle_deep.py --out data/analysis
"""

import argparse
from pathlib import Path
from datetime import date, datetime
import pandas as pd
import numpy as np

OUT_DIR = Path("data/analysis")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_trades(fname: str) -> pd.DataFrame:
    p = OUT_DIR / fname
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["expiry_date", "trade_date"])
    df["expiry_date"] = pd.to_datetime(df["expiry_date"]).dt.date
    df["trade_date"]  = pd.to_datetime(df["trade_date"]).dt.date
    df["days_to_expiry"] = (pd.to_datetime(df["expiry_date"]) - pd.to_datetime(df["trade_date"])).dt.days
    # Cycle phase: bucket by days to expiry
    bins   = [0, 5, 10, 15, 20, 25, 31]
    labels = ["D1-5 (expiry wk)", "D6-10", "D11-15", "D16-20", "D21-25", "D26-30"]
    df["cycle_phase"] = pd.cut(df["days_to_expiry"], bins=bins, labels=labels, right=True)
    # Premium change: <1 = contraction (profit), >1 = expansion (loss)
    df["premium_ratio"] = df["exit_premium"] / df["total_premium"]
    df["contracted"]    = df["premium_ratio"] < 1.0
    return df


def win_stats(g: pd.DataFrame) -> dict:
    if g.empty:
        return {}
    n    = len(g)
    wins = (g["pnl_pct"] > 0).sum()
    return {
        "n":           n,
        "win_pct":     round(wins / n * 100, 1),
        "avg_pnl_pct": round(g["pnl_pct"].mean(), 2),
        "avg_lot_pnl": round(g["pnl_per_lot"].mean(), 0),
        "sl_pct":      round((g["exit_reason"] == "SL").sum() / n * 100, 1),
        "contraction_pct": round(g["contracted"].mean() * 100, 1),
        "avg_premium_ratio": round(g["premium_ratio"].mean(), 3),
    }


def fmt_row(r: dict) -> str:
    return (f"  N={r['n']:<4} Win={r['win_pct']}%  AvgPnL={r['avg_pnl_pct']:>6}%  "
            f"SL={r['sl_pct']}%  Contract={r['contraction_pct']}%  Lot=Rs{r['avg_lot_pnl']:.0f}")


# ---------------------------------------------------------------------------
# 1. Cycle phase analysis (aggregated across all expiries)
# ---------------------------------------------------------------------------

def cycle_phase_analysis(df: pd.DataFrame, label: str):
    print(f"\n{'='*65}")
    print(f"  {label} — CYCLE PHASE ANALYSIS (days before expiry)")
    print(f"{'='*65}")
    print(f"  {'Phase':<18} {'N':<5} {'Win%':<7} {'AvgPnL%':<9} {'SL%':<6} {'Contract%':<11} {'Avg PremRatio'}")
    print(f"  {'-'*62}")

    phase_order = ["D26-30", "D21-25", "D16-20", "D11-15", "D6-10", "D1-5 (expiry wk)"]
    for phase in phase_order:
        g = df[df["cycle_phase"] == phase]
        if len(g) < 5:
            continue
        s = win_stats(g)
        print(f"  {phase:<18} {s['n']:<5} {s['win_pct']:<7} {s['avg_pnl_pct']:<9} "
              f"{s['sl_pct']:<6} {s['contraction_pct']:<11} {s['avg_premium_ratio']}")

    print(f"\n  Best entry times WITHIN each phase:")
    for phase in phase_order:
        g = df[df["cycle_phase"] == phase]
        if len(g) < 5:
            continue
        grp = g.groupby("entry_time").apply(win_stats).dropna()
        grp_df = pd.DataFrame(list(grp)).assign(entry_time=grp.index)
        grp_df = grp_df[grp_df["n"] >= 3].sort_values("win_pct", ascending=False).head(3)
        if grp_df.empty:
            continue
        print(f"\n  {phase}:")
        for _, r in grp_df.iterrows():
            print(f"    {r['entry_time']}  win={r['win_pct']}%  avgPnL={r['avg_pnl_pct']}%  n={int(r['n'])}")


# ---------------------------------------------------------------------------
# 2. Per-expiry best slots
# ---------------------------------------------------------------------------

def per_expiry_analysis(df: pd.DataFrame, label: str):
    print(f"\n{'='*65}")
    print(f"  {label} — PER EXPIRY BEST SLOTS")
    print(f"{'='*65}")

    expiries = sorted(df["expiry_date"].unique())
    rows = []

    for expiry in expiries:
        eg = df[df["expiry_date"] == expiry]
        total_trades = len(eg)
        overall_win  = round((eg["pnl_pct"] > 0).mean() * 100, 1)
        overall_pnl  = round(eg["pnl_pct"].mean(), 2)
        contract_pct = round(eg["contracted"].mean() * 100, 1)

        # Best single entry time for this expiry
        grp = eg.groupby("entry_time").apply(win_stats).dropna()
        if grp.empty:
            best_time = best_win = best_pnl = "N/A"
        else:
            grp_df = pd.DataFrame(list(grp)).assign(et=grp.index)
            grp_df = grp_df[grp_df["n"] >= 2].sort_values("win_pct", ascending=False)
            if grp_df.empty:
                best_time = best_win = best_pnl = "N/A"
            else:
                top = grp_df.iloc[0]
                best_time = top["et"]
                best_win  = f"{top['win_pct']}%"
                best_pnl  = f"{top['avg_pnl_pct']}%"

        # Best cycle phase for this expiry
        phase_grp = eg.groupby("cycle_phase", observed=True).apply(win_stats).dropna()
        if not phase_grp.empty:
            ph_df = pd.DataFrame(list(phase_grp)).assign(phase=phase_grp.index)
            ph_df = ph_df[ph_df["n"] >= 3].sort_values("win_pct", ascending=False)
            best_phase = ph_df.iloc[0]["phase"] if not ph_df.empty else "N/A"
        else:
            best_phase = "N/A"

        rows.append({
            "expiry":        str(expiry),
            "total_trades":  total_trades,
            "overall_win%":  overall_win,
            "overall_pnl%":  overall_pnl,
            "contract%":     contract_pct,
            "best_time":     best_time,
            "best_time_win": best_win,
            "best_time_pnl": best_pnl,
            "best_phase":    best_phase,
        })

        print(f"\n  {expiry}  ({total_trades} trades | overall win={overall_win}% | pnl={overall_pnl}% | contract={contract_pct}%)")
        print(f"    Best time : {best_time}  win={best_win}  pnl={best_pnl}")
        print(f"    Best phase: {best_phase}")

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. Day-of-week × cycle-phase heatmap
# ---------------------------------------------------------------------------

def dow_phase_heatmap(df: pd.DataFrame, label: str):
    print(f"\n{'='*65}")
    print(f"  {label} — WIN% HEATMAP  (Day × Cycle Phase)")
    print(f"{'='*65}")

    days   = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    phases = ["D26-30", "D21-25", "D16-20", "D11-15", "D6-10", "D1-5 (expiry wk)"]

    # Header
    print(f"  {'Day':<11}", end="")
    for p in phases:
        print(f" {p[:6]:>8}", end="")
    print()
    print(f"  {'-'*70}")

    for dow in days:
        print(f"  {dow:<11}", end="")
        for phase in phases:
            g = df[(df["day_of_week"] == dow) & (df["cycle_phase"] == phase)]
            if len(g) < 3:
                print(f"  {'  -':>8}", end="")
            else:
                wp = round((g["pnl_pct"] > 0).mean() * 100)
                print(f"  {wp:>7}%", end="")
        print()

    print(f"\n  (Win% shown, '-' = fewer than 3 trades)")


# ---------------------------------------------------------------------------
# 4. Entry time heatmap within monthly cycle
# ---------------------------------------------------------------------------

def time_phase_heatmap(df: pd.DataFrame, label: str):
    print(f"\n{'='*65}")
    print(f"  {label} — BEST ENTRY TIMES by CYCLE PHASE")
    print(f"{'='*65}")

    phases = ["D26-30", "D21-25", "D16-20", "D11-15", "D6-10", "D1-5 (expiry wk)"]
    for phase in phases:
        g = df[df["cycle_phase"] == phase]
        if len(g) < 5:
            continue
        grp = g.groupby(["day_of_week", "entry_time"]).apply(win_stats).dropna()
        if grp.empty:
            continue
        grp_df = pd.DataFrame(list(grp))
        grp_df[["day_of_week","entry_time"]] = pd.DataFrame(list(grp.index), columns=["day_of_week","entry_time"])
        grp_df = grp_df[grp_df["n"] >= 2].sort_values("win_pct", ascending=False).head(5)
        if grp_df.empty:
            continue
        print(f"\n  Phase {phase}:")
        print(f"  {'Day':<11} {'Time':<6} {'N':<4} {'Win%':<7} {'AvgPnL%':<9} {'Contract%'}")
        for _, r in grp_df.iterrows():
            print(f"  {r['day_of_week']:<11} {r['entry_time']:<6} {int(r['n']):<4} {r['win_pct']:<7} {r['avg_pnl_pct']:<9} {r['contraction_pct']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "ALL"], default="ALL")
    parser.add_argument("--out", default="data/analysis")
    args = parser.parse_args()

    global OUT_DIR
    OUT_DIR = Path(args.out)

    underlyings = ["NIFTY", "BANKNIFTY"] if args.underlying == "ALL" else [args.underlying]

    all_per_expiry = []

    for und in underlyings:
        df_m = load_trades(f"{und.lower()}_monthly_trades.csv")
        df_w = load_trades(f"{und.lower()}_weekly_trades.csv")

        for df, etype in [(df_m, "Monthly"), (df_w, "Weekly")]:
            if df.empty:
                print(f"\n  No data for {und} {etype}")
                continue

            label = f"{und} {etype}"
            cycle_phase_analysis(df, label)
            pe_df = per_expiry_analysis(df, label)
            all_per_expiry.append(pe_df.assign(underlying=und, expiry_type=etype))
            dow_phase_heatmap(df, label)
            time_phase_heatmap(df, label)

    # Save per-expiry CSV
    if all_per_expiry:
        out = pd.concat(all_per_expiry, ignore_index=True)
        out.to_csv(OUT_DIR / "per_expiry_best_slots.csv", index=False)
        print(f"\n\nSaved: {OUT_DIR}/per_expiry_best_slots.csv")

    print("\nDone.")


if __name__ == "__main__":
    main()
