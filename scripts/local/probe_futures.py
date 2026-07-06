import json, urllib.request, urllib.parse, gzip

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

# Download NSE FO instruments master
print("Downloading NSE FO instruments master...")
# Try multiple instrument master URLs
urls = [
    'https://assets.upstox.com/market-quote/instruments/exchange/NSE_FO.json.gz',
    'https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz',
]
instruments = []
for url in urls:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        try:
            data = gzip.decompress(raw)
        except Exception:
            data = raw
        instruments = json.loads(data)
        print(f"Loaded {len(instruments)} instruments from {url.split('/')[-1]}")
        break
    except Exception as e:
        print(f"  {url.split('/')[-1]}: {e}")

# Also try the API endpoint
if not instruments:
    print("Trying API instruments endpoint...")
    try:
        r = api_get('/instruments', {'exchange': 'NSE_FO', 'segment': 'NSE_FO'})
        instruments = r.get('data', [])
        print(f"Got {len(instruments)} instruments from API")
    except Exception as e:
        print(f"API endpoint: {e}")

# Find NIFTY and BANKNIFTY monthly futures (current/near expiry)
for symbol_filter, bank_filter in [("NIFTY", False), ("BANKNIFTY", True)]:
    fut = [i for i in instruments
           if i.get('instrument_type') == 'FUTIDX'
           and symbol_filter in i.get('tradingsymbol', '')
           and ('BANK' in i.get('tradingsymbol', '')) == bank_filter]
    print(f"\n{symbol_filter} futures ({len(fut)} contracts):")
    for f in sorted(fut, key=lambda x: x.get('expiry', '')):
        print(f"  {f.get('tradingsymbol'):<20} key={f.get('instrument_key')}  expiry={f.get('expiry')}")

# Try fetching historical data for a current NIFTY futures contract
print("\n\n--- Test: historical candle for current NIFTY futures ---")
fut_nifty = [i for i in instruments
             if i.get('instrument_type') == 'FUTIDX'
             and 'NIFTY' in i.get('tradingsymbol', '')
             and 'BANK' not in i.get('tradingsymbol', '')]
if fut_nifty:
    key = fut_nifty[0].get('instrument_key')
    print(f"Trying key: {key}")
    try:
        r = api_get(f'/historical-candle/{urllib.parse.quote(key, safe="")}/day/2026-06-13/2026-06-01')
        candles = r.get('data', {}).get('candles', [])
        print(f"Got {len(candles)} candles. Sample: {candles[:2]}")
    except Exception as e:
        print(f"Error: {e}")

# Try expired instruments candle endpoint with futures key format
print("\n--- Test: expired-instruments candle for Dec 2025 NIFTY FUT ---")
# Find NIFTY Dec 2025 futures in the master (may be expired)
dec_fut = [i for i in instruments
           if i.get('instrument_type') == 'FUTIDX'
           and 'NIFTY' in i.get('tradingsymbol', '')
           and 'BANK' not in i.get('tradingsymbol', '')
           and '2025' in str(i.get('expiry', ''))]
if dec_fut:
    key = dec_fut[0].get('instrument_key')
    print(f"Found: {dec_fut[0].get('tradingsymbol')} key={key}")
else:
    print("No 2025 NIFTY futures in current instruments master (they're expired)")
    # Construct key manually using known format NSE_FO|token|DD-MM-YYYY
    print("Will need to use expired-instruments endpoint")
