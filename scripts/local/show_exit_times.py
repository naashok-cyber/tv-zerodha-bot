import pandas as pd

configs = [
    ("data/analysis/nifty_monthly_trades.csv",   "NIFTY Monthly",    [("Friday","11:30"),("Monday","13:45"),("Monday","13:00")]),
    ("data/analysis/banknifty_weekly_trades.csv", "BANKNIFTY Weekly", [("Wednesday","09:15"),("Wednesday","09:45"),("Tuesday","13:30")]),
    ("data/analysis/banknifty_monthly_trades.csv","BANKNIFTY Monthly",[("Thursday","12:15"),("Tuesday","14:00")]),
    ("data/analysis/nifty_weekly_trades.csv",     "NIFTY Weekly",     [("Thursday","09:15"),("Thursday","09:30"),("Wednesday","09:15")]),
]

for fname, label, slots in configs:
    try:
        df = pd.read_csv(fname)
    except Exception:
        continue
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    for dow, etime in slots:
        g = df[(df["day_of_week"] == dow) & (df["entry_time"] == etime)]
        if len(g) < 3:
            continue
        exits = g["exit_reason"].value_counts()
        mode_exit = g["exit_time"].mode()
        mode_str = mode_exit[0] if len(mode_exit) else "N/A"

        # Compute avg hold in minutes
        def to_min(t):
            try:
                h, m = str(t).split(":")
                return int(h)*60 + int(m)
            except:
                return None
        entry_min = to_min(etime)
        g2 = g.copy()
        g2["exit_min"] = g2["exit_time"].apply(to_min)
        g2 = g2.dropna(subset=["exit_min"])
        avg_hold = int(g2["exit_min"].mean()) - entry_min if entry_min else 0

        wins = g[g["pnl_pct"] > 0]
        loss = g[g["pnl_pct"] <= 0]

        print(f"\n  Entry: {dow} {etime}  |  {len(g)} trades  |  avg hold ~{avg_hold} min  |  typical exit: {mode_str}")
        print(f"  Exits -> EOD:{exits.get('EOD',0)}  SL:{exits.get('SL',0)}  Target:{exits.get('TARGET',0)}")
        print(f"  Win avg PnL: {wins['pnl_pct'].mean():.1f}%  |  Loss avg PnL: {loss['pnl_pct'].mean():.1f}%  |  Avg lot PnL: Rs{g['pnl_per_lot'].mean():.0f}")
        print(f"  Avg entry premium: {g['total_premium'].mean():.0f}  |  Avg exit premium: {g['exit_premium'].mean():.0f}")
