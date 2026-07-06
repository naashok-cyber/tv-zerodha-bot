import json, urllib.request, urllib.parse, gzip
from datetime import datetime

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def api_get(endpoint, params=None):
    url = BASE + endpoint
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json', 'User-Agent': UA})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# 1. Check option contract instrument keys to understand format
print("=== Option contract key format (NIFTY Dec 2025) ===")
r = api_get('/expired-instruments/option/contract', {
    'instrument_key': 'NSE_INDEX|Nifty 50',
    'expiry_date': '2025-12-25'
})
contracts = r.get('data', [])
print(f"Got {len(contracts)} contracts")
for c in contracts[:3]:
    print(f"  key={c.get('instrument_key')}  symbol={c.get('trading_symbol')}")

# 2. Try NSE_INDEX historical candle (spot price - doesn't need token lookup)
print("\n=== NSE_INDEX spot data (NIFTY 50) ===")
try:
    # Use the /v2/historical-candle for index data (not expired instruments)
    key = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
    r2 = api_get(f'/historical-candle/{key}/day/2025-12-25/2025-11-25')
    candles = r2.get('data', {}).get('candles', [])
    print(f"Got {len(candles)} day candles for NIFTY spot (Nov-Dec 2025)")
    if candles:
        print(f"  Sample: {candles[0]}")
except Exception as e:
    print(f"  Error: {e}")

# 3. Try 5-min index candle for spot NIFTY
print("\n=== NSE_INDEX 5-min spot data (NIFTY 50, last week of Dec 2025) ===")
try:
    key = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
    r3 = api_get(f'/historical-candle/{key}/5minute/2025-12-25/2025-12-22')
    candles = r3.get('data', {}).get('candles', [])
    print(f"Got {len(candles)} 5-min candles")
    if candles:
        print(f"  First: {candles[0]}")
        print(f"  Last:  {candles[-1]}")
except Exception as e:
    print(f"  Error: {e}")

# 4. Try using expired options key format to get futures
# Options key example: NSE_FO|XXXXXX|DD-MM-YYYY
# Futures key would be: NSE_FO|XXXXXX|DD-MM-YYYY (same format, different token)
# Try to get futures instrument key from option contract key pattern
if contracts:
    sample_key = contracts[0].get('instrument_key', '')
    print(f"\n=== Option instrument key format: {sample_key} ===")
    # Key format: NSE_FO|<token>|<DD-MM-YYYY>
    parts = sample_key.split('|')
    print(f"  Parts: {parts}")

# 5. Try the expired instruments historical candle with index key (spot)
print("\n=== Try expired-instruments candle with NSE_INDEX key ===")
try:
    key = 'NSE_INDEX|Nifty 50'
    encoded_key = urllib.parse.quote(key, safe='')
    r5 = api_get(f'/expired-instruments/historical-candle/{encoded_key}/5minute/2025-12-25/2025-12-22')
    candles = r5.get('data', {}).get('candles', [])
    print(f"Got {len(candles)} candles")
    if candles:
        print(f"  Sample: {candles[0]}")
except Exception as e:
    print(f"  Error: {e}")
