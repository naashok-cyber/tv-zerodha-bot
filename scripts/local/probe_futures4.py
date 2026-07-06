import json, urllib.request, urllib.parse, time

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def api_get(endpoint, params=None, base=BASE):
    url = base + endpoint
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json', 'User-Agent': UA})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

time.sleep(3)

# Test 1: NSE_INDEX 5-min intraday (single day)
key = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
print('Test 1: NSE_INDEX 5-min, 1-day range (2025-12-24)')
try:
    r = api_get(f'/historical-candle/{key}/5minute/2025-12-24/2025-12-24')
    candles = r.get('data', {}).get('candles', [])
    print(f'  OK: {len(candles)} candles, sample: {candles[0] if candles else None}')
except Exception as e:
    print(f'  Error: {str(e)[:120]}')

time.sleep(1)

# Test 2: Current NIFTY FUT 5-min
key2 = urllib.parse.quote('NSE_FO|61093', safe='')
print('Test 2: Current NIFTY FUT (NSE_FO|61093) 5-min Jun 2026')
try:
    r2 = api_get(f'/historical-candle/{key2}/5minute/2026-06-13/2026-06-01')
    candles2 = r2.get('data', {}).get('candles', [])
    print(f'  OK: {len(candles2)} candles, sample: {candles2[0] if candles2 else None}')
except Exception as e:
    print(f'  Error: {str(e)[:120]}')

time.sleep(1)

# Test 3: v3 API for index
print('Test 3: v3 API NSE_INDEX 5-min')
try:
    key3 = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
    r3 = api_get(f'/historical-candle/{key3}/5minute/2025-12-24/2025-12-24', base='https://api.upstox.com/v3')
    candles3 = r3.get('data', {}).get('candles', [])
    print(f'  OK: {len(candles3)} candles')
except Exception as e:
    print(f'  Error: {str(e)[:120]}')

time.sleep(1)

# Test 4: Option contracts for May 2026 to understand key format
print('Test 4: NIFTY May 2026 option contracts (to see key format)')
try:
    r4 = api_get('/expired-instruments/option/contract', {
        'instrument_key': 'NSE_INDEX|Nifty 50',
        'expiry_date': '2026-05-29'
    })
    contracts = r4.get('data', [])
    print(f'  {len(contracts)} contracts')
    for c in contracts[:3]:
        ikey = c.get('instrument_key', '')
        sym = c.get('trading_symbol', '')
        print(f'  key={ikey}  sym={sym}')
except Exception as e:
    print(f'  Error: {str(e)[:120]}')

time.sleep(1)

# Test 5: Try expired instruments historical candle for a known option
# to understand if futures key works the same way
print('Test 5: expired-instruments candle for NIFTY 24500 CE Dec 2025')
try:
    r5_contracts = api_get('/expired-instruments/option/contract', {
        'instrument_key': 'NSE_INDEX|Nifty 50',
        'expiry_date': '2025-12-25'
    })
    c5 = r5_contracts.get('data', [])
    print(f'  Dec 2025 option contracts: {len(c5)}')
    # try expiry date as last Thursday of Dec 2025 = Dec 25
    # NIFTY monthly was Dec 25, 2025? Let me check another date
except Exception as e:
    print(f'  Error: {str(e)[:120]}')

# The NIFTY monthly expiry for Dec 2025 was actually 2025-12-25 (Thursday)
# But our data shows 2025-12-30 as the expiry. Let me check.
print('\nNIFTY monthly expiries from our CSV:')
import pandas as pd
df = pd.read_csv('data/analysis/nifty_monthly_trades.csv')
expiries = sorted(df['expiry_date'].unique())
print(expiries[:6])
