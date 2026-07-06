import pandas as pd
from pathlib import Path

OUT = Path("data/analysis")
LAST6 = ["2025-12-30", "2026-01-27", "2026-02-24", "2026-03-30", "2026-04-28", "2026-05-26"]

# Also try BANKNIFTY last 6 months
BN_LAST6 = ["2025-12-30", "2026-01-27", "2026-02-24", "2026-03-30", "2026-04-28", "2026-05-26"]

PHASE_ORDER = ["D26-30", "D21-25", "D16-20", "D11-15", "D6-10", "D1-5 (expiry wk)"]

def load(fname):
    p = OUT / fname
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["expiry_date", "trade_date"])
    df["expiry_date"] = pd.to_datetime(df["expiry_date"]).dt.date
    df["trade_date"]  = pd.to_datetime(df["trade_date"]).dt.date
    df["days_to_expiry"] = (pd.to_datetime(df["expiry_date"]) - pd.to_datetime(df["trade_date"])).dt.days
    bins   = [0, 5, 10, 15, 20, 25, 31]
    labels = ["D1-5 (expiry wk)", "D6-10", "D11-15", "D16-20", "D21-25", "D26-30"]
    df["cycle_phase"] = pd.cut(df["days_to_expiry"], bins=bins, labels=labels, right=True)
    df["contracted"] = df["exit_premium"] < df["total_premium"]
    return df

def show_expiry(df, expiry_str, label):
    eg = df[df["expiry_date"].astype(str) == expiry_str]
    if eg.empty:
        print(f"  {expiry_str}: no data")
        return

    n_total = len(eg)
    win_pct = round((eg["pnl_pct"] > 0).mean() * 100, 1)
    avg_pnl = round(eg["pnl_pct"].mean(), 2)
    contract_pct = round(eg["contracted"].mean() * 100, 1)

    print(f"\n{'='*65}")
    print(f"  {label} — Expiry {expiry_str}")
    print(f"  Overall: {n_total} trades | Win {win_pct}% | AvgPnL {avg_pnl}% | Contract {contract_pct}%")
    print(f"{'='*65}")

    # Phase breakdown
    print(f"\n  {'Phase':<20} {'N':<5} {'Win%':<7} {'AvgPnL%':<9} {'SL%':<6} {'Contract%'}")
    print(f"  {'-'*55}")
    for phase in PHASE_ORDER:
        g = eg[eg["cycle_phase"] == phase]
        if len(g) < 2:
            continue
        n = len(g)
        win = round((g["pnl_pct"] > 0).mean() * 100, 1)
        apnl = round(g["pnl_pct"].mean(), 2)
        sl = round((g["exit_reason"] == "SL").sum() / n * 100, 1)
        cont = round(g["contracted"].mean() * 100, 1)
        print(f"  {phase:<20} {n:<5} {win:<7} {apnl:<9} {sl:<6} {cont}")

    # Best entry times per phase (single expiry so n=1 per day×time — group by time only)
    print(f"\n  Best entry TIMES per phase (top 3 by PnL%):")
    for phase in PHASE_ORDER:
        g = eg[eg["cycle_phase"] == phase]
        if len(g) < 3:
            continue
        grp = g.groupby("entry_time").agg(
            n=("pnl_pct", "count"),
            win_pct=("pnl_pct", lambda x: round((x > 0).mean() * 100, 1)),
            avg_pnl=("pnl_pct", lambda x: round(x.mean(), 2)),
            avg_lot=("pnl_per_lot", lambda x: round(x.mean(), 0)),
        ).reset_index()
        # Also show best day within that time
        top = grp.sort_values(["win_pct", "avg_pnl"], ascending=False).head(3)
        if top.empty:
            continue
        # Find best day for each top time
        print(f"\n    {phase}  (days in phase: {', '.join(g['day_of_week'].unique())})")
        print(f"    {'Time':<6} {'Win%':<7} {'AvgPnL%':<9} {'AvgLot':<10} Best Day")
        for _, r in top.iterrows():
            gt = g[g["entry_time"] == r["entry_time"]]
            best_day = gt.groupby("day_of_week")["pnl_pct"].mean().idxmax() if not gt.empty else "-"
            print(f"    {r['entry_time']:<6} {r['win_pct']:<7} {r['avg_pnl']:<9} Rs{r['avg_lot']:<8.0f} {best_day}")

for und, fname, expiries in [
    ("NIFTY",     "nifty_monthly_trades.csv",     LAST6),
    ("BANKNIFTY", "banknifty_monthly_trades.csv",  BN_LAST6),
]:
    df = load(fname)
    if df.empty:
        print(f"\nNo data for {und} monthly")
        continue
    print(f"\n\n{'#'*65}")
    print(f"  {und} MONTHLY — LAST 6 EXPIRIES (Dec 2025 to May 2026)")
    print(f"{'#'*65}")
    for exp in expiries:
        show_expiry(df, exp, und)
