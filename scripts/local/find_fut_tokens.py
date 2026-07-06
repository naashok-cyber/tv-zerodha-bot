"""
Find NIFTY and BANKNIFTY futures instrument tokens from Upstox instruments master,
then discover expired contract tokens by testing the API.

The expired_instrument_key format: NSE_FO|{token}|{DD-MM-YYYY}
Example from docs: NSE_FO|54452|24-04-2025  (NIFTY APR 2025 FUT)
"""
import json, urllib.request, urllib.parse, gzip, time

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def api_get(path):
    req = urllib.request.Request(BASE + path, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': UA
    })
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            candles = data.get('data', {}).get('candles', [])
            return True, len(candles), candles[:1]
    except urllib.error.HTTPError as e:
        return False, 0, e.read().decode()[:100]

# Download instruments master
print("Loading instruments master (complete.json.gz)...")
req = urllib.request.Request(
    'https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz',
    headers={'User-Agent': UA}
)
with urllib.request.urlopen(req, timeout=30) as r:
    instruments = json.loads(gzip.decompress(r.read()))
print(f"Loaded {len(instruments):,} instruments")

# Find NIFTY and BANKNIFTY FUT contracts using correct field names
print("\n=== NIFTY FUT contracts in master ===")
nifty_futs = []
bn_futs = []
for inst in instruments:
    ts = inst.get('trading_symbol', '')
    seg = inst.get('segment', '')
    itype = inst.get('instrument_type', '')
    if seg == 'NSE_FO' and itype == 'FUT':
        underlying = inst.get('underlying_symbol', '')
        if underlying == 'NIFTY':
            nifty_futs.append(inst)
            print(f"  NIFTY: {ts}  token={inst.get('exchange_token')}  key={inst.get('instrument_key')}  expiry_ms={inst.get('expiry')}")
        elif underlying == 'BANKNIFTY':
            bn_futs.append(inst)
            print(f"  BANKNIFTY: {ts}  token={inst.get('exchange_token')}  key={inst.get('instrument_key')}  expiry_ms={inst.get('expiry')}")

# Convert expiry timestamps to dates
from datetime import date, datetime, timedelta
def ms_to_date(ms):
    return datetime.utcfromtimestamp(ms / 1000).date() if ms else None

print("\n=== Current contracts with expiry dates ===")
for f in nifty_futs:
    exp = ms_to_date(f.get('expiry'))
    print(f"  NIFTY FUT {exp}  exchange_token={f.get('exchange_token')}  key={f.get('instrument_key')}")
for f in bn_futs:
    exp = ms_to_date(f.get('expiry'))
    print(f"  BANKNIFTY FUT {exp}  exchange_token={f.get('exchange_token')}  key={f.get('instrument_key')}")

# Now test the API with the known example: NSE_FO|54452|24-04-2025 (NIFTY APR 2025)
print("\n=== Test: docs example NIFTY APR 2025 FUT (NSE_FO|54452|24-04-2025) ===")
key_ex = urllib.parse.quote('NSE_FO|54452|24-04-2025', safe='')
ok, n, sample = api_get(f'/expired-instruments/historical-candle/{key_ex}/5minute/2025-04-24/2025-04-24')
print(f"  Status: {'OK' if ok else 'FAIL'}  n={n}  {str(sample)[:100]}")
time.sleep(1.2)

# Test with current NIFTY FUT tokens (to see the correct expired key format)
if nifty_futs:
    for f in nifty_futs[:2]:
        tok = f.get('exchange_token')
        exp = ms_to_date(f.get('expiry'))
        if exp and tok:
            exp_str = exp.strftime('%d-%m-%Y')
            key = urllib.parse.quote(f'NSE_FO|{tok}|{exp_str}', safe='')
            print(f"\n=== Test current NIFTY FUT: NSE_FO|{tok}|{exp_str} ===")
            ok, n, sample = api_get(f'/expired-instruments/historical-candle/{key}/5minute/2026-06-13/2026-06-12')
            print(f"  Status: {'OK' if ok else 'FAIL'}  n={n}  {str(sample)[:100]}")
            time.sleep(1.2)

# Use the docs example (token=54452, Apr 2025) to extrapolate other month tokens
# Apr 2025 token: 54452, expiry: 24-04-2025
# Current Jul 2026 tokens from master
print("\n=== Token pattern analysis ===")
print(f"  NIFTY FUT Apr 2025 (from docs): token=54452  expiry=2025-04-24")
for f in nifty_futs:
    exp = ms_to_date(f.get('expiry'))
    tok = f.get('exchange_token')
    print(f"  NIFTY FUT {exp} (from master): token={tok}")

# Extrapolate: compute approximate tokens for each month between Apr 2025 and current
# We'll verify each one against the API
print("\nWill test token discovery for NIFTY monthly expiries...")
