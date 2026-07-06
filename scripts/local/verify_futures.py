import pandas as pd

for und, fname in [
    ('NIFTY',     'data/nifty_futures/nifty_spot_daily.csv'),
    ('BANKNIFTY', 'data/banknifty_futures/banknifty_spot_daily.csv'),
]:
    df = pd.read_csv(fname)
    print(f"{und}: {len(df)} rows, {df['expiry'].nunique()} monthly expiries")
    print(f"  Date range: {df['date'].min()} to {df['date'].max()}")
    print(f"  Expiries (first 4): {sorted(df['expiry'].unique())[:4]}")
    print(f"  Last expiry sample (days_to_expiry, close):")
    last_exp = df[df['expiry'] == df['expiry'].max()]
    for _, r in last_exp[['date','close','days_to_expiry']].iterrows():
        print(f"    {r['date']}  close={r['close']:,.0f}  dte={int(r['days_to_expiry'])}")
    print()
