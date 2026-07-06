import pandas as pd
from pathlib import Path
from datetime import date

OUT = Path("data/analysis")

def load(fname):
    p = OUT / fname
    if not p.exists(): return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["expiry_date","trade_date"])
    df["expiry_date"] = pd.to_datetime(df["expiry_date"]).dt.date
    df["trade_date"]  = pd.to_datetime(df["trade_date"]).dt.date
    df["days_to_expiry"] = (pd.to_datetime(df["expiry_date"]) - pd.to_datetime(df["trade_date"])).dt.days
    # Weekly phase: shorter buckets (max 15 days to expiry for weeklies)
    bins   = [0, 3, 5, 7, 10, 15]
    labels = ["D1-3", "D4-5", "D6-7", "D8-10", "D11-15"]
    df["phase"] = pd.cut(df["days_to_expiry"], bins=bins, labels=labels, right=True)
    return df

def show_top(df, expiry_str, label, n=7):
    eg = df[df["expiry_date"].astype(str) == expiry_str].copy()
    if eg.empty:
        print(f"  {expiry_str}: no data")
        return

    overall_win = round((eg["pnl_pct"] > 0).mean() * 100, 1)
    overall_pnl = round(eg["pnl_pct"].mean(), 2)

    grp = eg.groupby(["entry_time","day_of_week","phase"], observed=True).agg(
        n        = ("pnl_pct","count"),
        win_pct  = ("pnl_pct", lambda x: round((x>0).mean()*100,1)),
        avg_pnl  = ("pnl_pct", lambda x: round(x.mean(),2)),
        avg_lot  = ("pnl_per_lot", lambda x: round(x.mean(),0)),
        sl_count = ("exit_reason", lambda x: (x=="SL").sum()),
    ).reset_index()

    grp_no_sl = grp[grp["sl_count"] == 0]
    top = grp_no_sl.sort_values(["win_pct","avg_pnl"], ascending=False).head(n)

    print(f"\n{'='*70}")
    print(f"  {label}  |  Expiry {expiry_str}  |  Overall: Win {overall_win}%  AvgPnL {overall_pnl}%")
    print(f"{'='*70}")
    print(f"  {'#':<3} {'Time':<6} {'Day':<11} {'Phase':<8} {'N':<4} {'Win%':<7} {'AvgPnL%':<9} {'AvgLot'}")
    print(f"  {'-'*62}")
    if top.empty:
        print("  (all slots had SL hits)")
        return
    for rank, (_, r) in enumerate(top.iterrows(), 1):
        print(f"  {rank:<3} {r['entry_time']:<6} {r['day_of_week']:<11} {str(r['phase']):<8} "
              f"{int(r['n']):<4} {r['win_pct']:<7} {r['avg_pnl']:<9} Rs{r['avg_lot']:.0f}")

for und, fname in [
    ("NIFTY",     "nifty_weekly_trades.csv"),
    ("BANKNIFTY", "banknifty_weekly_trades.csv"),
]:
    df = load(fname)
    if df.empty:
        print(f"\nNo data for {und} weekly")
        continue
    expiries = sorted(df["expiry_date"].unique())
    # Only completed expiries (before Jun 2026 since Jun is current/in-progress)
    expiries = [e for e in expiries if e < date(2026, 6, 1)]
    last12 = [str(e) for e in expiries[-12:]]
    print(f"\n\n{'#'*70}")
    print(f"  {und} WEEKLY - TOP 7 TRADES PER EXPIRY (last 12 expiries, no SL hits)")
    print(f"  Data available: {last12[0]} to {last12[-1]}")
    print(f"{'#'*70}")
    for exp in last12:
        show_top(df, exp, und)
