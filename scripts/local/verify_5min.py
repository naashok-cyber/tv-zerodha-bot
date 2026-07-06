import pandas as pd

for und, fname in [
    ('NIFTY',     'data/nifty_futures/nifty_5min_implied.csv'),
    ('BANKNIFTY', 'data/banknifty_futures/banknifty_5min_implied.csv'),
]:
    df = pd.read_csv(fname)
    monthly = df[df['expiry_type'] == 'monthly']
    # Filter to 30 days before expiry
    m30 = monthly[monthly['days_to_expiry'] <= 30]

    print(f"\n{und} 5-min implied underlying:")
    print(f"  Total bars (all DTE): {len(monthly):,}")
    print(f"  Bars within 30 DTE:   {len(m30):,}")
    print(f"  Monthly expiries:     {monthly['expiry'].nunique()}")
    print(f"  Date range:           {monthly['date'].min()} to {monthly['date'].max()}")
    print(f"  Price range:          {monthly['implied_fwd'].min():,.0f} - {monthly['implied_fwd'].max():,.0f}")
    print(f"\n  Sample (last 5 bars of {monthly['expiry'].max()} expiry):")
    last = monthly[monthly['expiry'] == monthly['expiry'].max()].tail(5)
    print(last[['date','time','implied_fwd','atm_strike','ce_price','pe_price','days_to_expiry']].to_string(index=False))
