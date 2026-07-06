import pandas as pd
from pathlib import Path

OUT = Path("data/analysis")
LAST6_NIFTY = ["2025-12-30","2026-01-27","2026-02-24","2026-03-30","2026-04-28","2026-05-26"]
LAST6_BN    = ["2025-12-30","2026-01-27","2026-02-24","2026-03-30","2026-04-28","2026-05-26"]

def load(fname):
    p = OUT / fname
    if not p.exists(): return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["expiry_date","trade_date"])
    df["expiry_date"] = pd.to_datetime(df["expiry_date"]).dt.date
    df["trade_date"]  = pd.to_datetime(df["trade_date"]).dt.date
    df["days_to_expiry"] = (pd.to_datetime(df["expiry_date"]) - pd.to_datetime(df["trade_date"])).dt.days
    bins   = [0,5,10,15,20,25,31]
    labels = ["D1-5","D6-10","D11-15","D16-20","D21-25","D26-30"]
    df["phase"] = pd.cut(df["days_to_expiry"], bins=bins, labels=labels, right=True)
    return df

def show_top(df, expiry_str, label, n=7):
    eg = df[df["expiry_date"].astype(str) == expiry_str].copy()
    if eg.empty:
        print(f"  {expiry_str}: no data")
        return

    overall_win = round((eg["pnl_pct"] > 0).mean() * 100, 1)
    overall_pnl = round(eg["pnl_pct"].mean(), 2)

    # Group by (entry_time, day_of_week, phase) — each combo = multiple days in that slot
    grp = eg.groupby(["entry_time","day_of_week","phase"], observed=True).agg(
        n        = ("pnl_pct","count"),
        win_pct  = ("pnl_pct", lambda x: round((x>0).mean()*100,1)),
        avg_pnl  = ("pnl_pct", lambda x: round(x.mean(),2)),
        avg_lot  = ("pnl_per_lot", lambda x: round(x.mean(),0)),
        avg_prem = ("total_premium", lambda x: round(x.mean(),0)),
        sl_count = ("exit_reason", lambda x: (x=="SL").sum()),
    ).reset_index()

    # Rank: first by win%, then by avg_pnl (no SL preferred)
    grp = grp[grp["sl_count"] == 0]          # exclude slots with any SL hit
    top = grp.sort_values(["win_pct","avg_pnl"], ascending=False).head(n)

    print(f"\n{'='*70}")
    print(f"  {label}  |  Expiry {expiry_str}  |  Overall: Win {overall_win}%  AvgPnL {overall_pnl}%")
    print(f"{'='*70}")
    print(f"  {'#':<3} {'Time':<6} {'Day':<11} {'Phase':<8} {'N':<4} {'Win%':<7} {'AvgPnL%':<9} {'SL':<4} {'AvgLot'}")
    print(f"  {'-'*65}")
    for rank, (_, r) in enumerate(top.iterrows(), 1):
        print(f"  {rank:<3} {r['entry_time']:<6} {r['day_of_week']:<11} {str(r['phase']):<8} "
              f"{int(r['n']):<4} {r['win_pct']:<7} {r['avg_pnl']:<9} {int(r['sl_count']):<4} Rs{r['avg_lot']:.0f}")

# ── Run ──────────────────────────────────────────────────────────────────────
for und, fname, expiries in [
    ("NIFTY",     "nifty_monthly_trades.csv",    LAST6_NIFTY),
    ("BANKNIFTY", "banknifty_monthly_trades.csv", LAST6_BN),
]:
    df = load(fname)
    if df.empty: continue
    print(f"\n\n{'#'*70}")
    print(f"  {und} MONTHLY — TOP 7 TRADES PER EXPIRY (last 6 months, no SL hits)")
    print(f"{'#'*70}")
    for exp in expiries:
        show_top(df, exp, und)
