"""
Clean probe - run after rate limit resets (wait 5+ min after killing downloads).
Tests:
  A) expired-instruments 5min for NSE_INDEX (spot)
  B) expired-instruments 5min for NSE_INDEX|Nifty Bank
  C) What intervals does expired-instruments support for NSE_INDEX?
  D) Can we find expired NIFTY FUT instrument keys?
  E) regular historical-candle supported intervals for current NIFTY FUT
"""
import json, urllib.request, urllib.parse, time, gzip

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def api(path):
    url = BASE + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json', 'User-Agent': UA
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return {}, f"HTTP {e.code}: {e.read().decode()[:200]}"

key_n  = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
key_bn = urllib.parse.quote('NSE_INDEX|Nifty Bank', safe='')
key_fut = urllib.parse.quote('NSE_FO|61093', safe='')

# A) expired-instruments intervals for NSE_INDEX
print("=== A) What intervals work for expired-instruments NSE_INDEX? ===")
for interval in ['1minute', '5minute', '10minute', '15minute', '30minute', '60minute', 'day']:
    data, err = api(f'/expired-instruments/historical-candle/{key_n}/{interval}/2025-12-24/2025-12-24')
    if err:
        print(f"  {interval:<12}: FAIL  {err[:80]}")
    else:
        candles = data.get('data', {}).get('candles', [])
        print(f"  {interval:<12}: OK  {len(candles)} candles, sample={str(candles[0])[:60] if candles else 'empty'}")
    time.sleep(1.2)

# B) expired-instruments NSE_INDEX 5min - longer range
print("\n=== B) expired-instruments NIFTY 5min full Dec 2025 (if A works) ===")
data, err = api(f'/expired-instruments/historical-candle/{key_n}/5minute/2025-12-26/2025-12-01')
if err:
    print(f"  FAIL: {err[:100]}")
else:
    candles = data.get('data', {}).get('candles', [])
    print(f"  OK: {len(candles)} candles (Dec 2025)")
    if candles:
        print(f"  First: {candles[0]}")
        print(f"  Last:  {candles[-1]}")
time.sleep(1.2)

# C) expired-instruments BANKNIFTY 5min
print("\n=== C) expired-instruments BANKNIFTY 5min Dec 2024 ===")
data, err = api(f'/expired-instruments/historical-candle/{key_bn}/5minute/2024-12-24/2024-12-01')
if err:
    print(f"  FAIL: {err[:100]}")
else:
    candles = data.get('data', {}).get('candles', [])
    print(f"  OK: {len(candles)} candles")
    if candles:
        print(f"  First: {candles[0]}")
time.sleep(1.2)

# D) Regular historical-candle for current NIFTY FUT - what intervals work?
print("\n=== D) Regular historical-candle current NIFTY FUT (NSE_FO|61093) ===")
for interval in ['1minute', '5minute', '30minute', '60minute', 'day']:
    data, err = api(f'/historical-candle/{key_fut}/{interval}/2026-06-13/2026-06-12')
    if err:
        msg = json.loads(err.split(':', 2)[-1])['errors'][0]['message'] if '{' in err else err
        print(f"  {interval:<12}: FAIL  {str(msg)[:70]}")
    else:
        candles = data.get('data', {}).get('candles', [])
        print(f"  {interval:<12}: OK  {len(candles)} candles")
    time.sleep(1.2)

# E) Try expired NSE_FO futures key formats
print("\n=== E) Try expired NIFTY FUT instrument keys ===")
# NIFTY FUT tokens - try to access using the expiry format
# Current token: 61093 (Jul 2026). Let's try some nearby tokens for expired contracts
# We can't know the exact token without the historical master, but let's try the expiry endpoint
# to see if futures expiries are listed
data, err = api('/expired-instruments/expiries?instrument_key=NSE_FO%7CNIFTY')
print(f"  NSE_FO|NIFTY expiries: {err[:100] if err else data.get('data','')[:100]}")
time.sleep(1.2)

# Try symbol-based key
data, err = api('/expired-instruments/expiries?instrument_key=NSE_FO%7CNifty+Fut')
print(f"  NSE_FO|Nifty Fut expiries: {err[:100] if err else str(data.get('data',''))[:100]}")
time.sleep(1.2)

# Try using option key to find futures - first get an option contract
data2, err2 = api('/expired-instruments/option/contract?instrument_key=NSE_INDEX%7CNifty+50&expiry_date=2025-12-26')
contracts = data2.get('data', []) if not err2 else []
print(f"\n  NIFTY Dec 2025 option contracts: {len(contracts)}")
if contracts:
    print(f"  Sample key: {contracts[0].get('instrument_key')}  sym={contracts[0].get('trading_symbol')}")
