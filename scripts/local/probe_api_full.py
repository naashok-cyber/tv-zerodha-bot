"""
Full probe of Upstox API for 5-min futures/index data.
Run after background downloads are stopped to avoid rate limits.
"""
import json, urllib.request, urllib.parse, time, gzip

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def get(path, base=BASE):
    url = base + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
        'User-Agent': UA
    })
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            candles = data.get('data', {}).get('candles', [])
            return 'OK', len(candles), candles[:2], data
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        return str(e.code), 0, body, {}

print("=" * 65)
print("  UPSTOX 5-MIN DATA CAPABILITY PROBE")
print("=" * 65)

# --- Test 1: expired-instruments for NSE_INDEX 5min ---
key_nifty = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
key_bn    = urllib.parse.quote('NSE_INDEX|Nifty Bank', safe='')

print("\n1. expired-instruments NSE_INDEX 5min (Dec 2025)")
status, n, sample, _ = get(f'/expired-instruments/historical-candle/{key_nifty}/5minute/2025-12-26/2025-12-01')
print(f"   Status: {status}  Candles: {n}")
if status == 'OK' and n > 0:
    print(f"   First: {sample[0]}")
    print(f"   Last:  {sample[-1] if len(sample)>1 else ''}")
else:
    print(f"   Body: {sample[:150]}")
time.sleep(1.5)

# --- Test 2: expired-instruments BANKNIFTY 5min ---
print("\n2. expired-instruments NSE_INDEX|Nifty Bank 5min (Dec 2025)")
status, n, sample, _ = get(f'/expired-instruments/historical-candle/{key_bn}/5minute/2025-12-24/2025-12-01')
print(f"   Status: {status}  Candles: {n}")
if n > 0:
    print(f"   First: {sample[0]}")
else:
    print(f"   Body: {str(sample)[:150]}")
time.sleep(1.5)

# --- Test 3: Try with 1minute interval ---
print("\n3. expired-instruments NSE_INDEX 1minute (Dec 2025, 1 day)")
status, n, sample, _ = get(f'/expired-instruments/historical-candle/{key_nifty}/1minute/2025-12-24/2025-12-24')
print(f"   Status: {status}  Candles: {n}")
if n > 0:
    print(f"   First: {sample[0]}")
else:
    print(f"   Body: {str(sample)[:150]}")
time.sleep(1.5)

# --- Test 4: Try expired futures via instrument key ---
# Approach: try the expired option key format but for futures
# NIFTY FUT Dec 2024 - we need to find the exchange token
# Load instrument master to see what futures tokens look like
print("\n4. Trying to find expired NIFTY FUT via expiries endpoint")
try:
    # Try with NSE_FO segment key for futures
    key_fut = urllib.parse.quote('NSE_FO|NIFTY', safe='')
    status, n, sample, full = get(f'/expired-instruments/expiries?instrument_key=NSE_FO%7CNIFTY')
    print(f"   NSE_FO|NIFTY expiries: {status}  data={str(full.get('data',''))[:100]}")
except Exception as e:
    print(f"   Error: {e}")
time.sleep(1.5)

# --- Test 5: Try fetching instrument master for expired futures ---
print("\n5. Try expired instruments with different underlying key for futures")
# NIFTY underlying as index - maybe 'NSE_INDEX|Nifty 50' also lists futures expiries
status, n, sample, full = get('/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty+50')
expiries = full.get('data', [])
print(f"   NSE_INDEX|Nifty 50 expiries: {status}  count={len(expiries)}")
if expiries:
    print(f"   Sample: {expiries[:5]} ... {expiries[-3:]}")
time.sleep(1.5)

# --- Test 6: Check what intervals the regular endpoint supports ---
print("\n6. Regular historical-candle supported intervals for NSE_FO futures")
key_current_fut = urllib.parse.quote('NSE_FO|61093', safe='')
for interval in ['1minute', '5minute', '10minute', '15minute', '30minute', '60minute', 'day']:
    status, n, sample, full = get(f'/historical-candle/{key_current_fut}/{interval}/2026-06-13/2026-06-12')
    err_msg = ''
    if status != 'OK':
        err_data = full.get('errors', [{}])
        err_msg = err_data[0].get('message', '')[:60] if err_data else ''
    print(f"   {interval:<10}: {status}  n={n}  {err_msg}")
    time.sleep(0.8)

# --- Test 7: Try v3 API for futures 5min ---
print("\n7. v3 API for NSE_FO futures 5min")
status, n, sample, _ = get(f'/historical-candle/{key_current_fut}/5minute/2026-06-13/2026-06-12', base='https://api.upstox.com/v3')
print(f"   v3 NSE_FO 5min: {status}  n={n}  {str(sample)[:100]}")
time.sleep(1.5)

# --- Test 8: Intraday endpoint for NSE_INDEX ---
print("\n8. Intraday endpoint (today's data)")
status, n, sample, _ = get(f'/historical-candle/intraday/{key_nifty}/5minute')
print(f"   NSE_INDEX intraday 5min: {status}  n={n}  {str(sample)[:100]}")
