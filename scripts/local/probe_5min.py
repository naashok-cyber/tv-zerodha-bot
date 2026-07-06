import urllib.request, urllib.parse, json, time

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def get(path):
    req = urllib.request.Request(BASE + path,
          headers={'Authorization': 'Bearer ' + token,
                   'Accept': 'application/json', 'User-Agent': UA})
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            candles = data.get('data', {}).get('candles', [])
            return 'OK', len(candles), candles[:1]
    except urllib.error.HTTPError as e:
        return str(e.code), 0, e.read().decode()[:200]

# Test 1: current NIFTY FUT (NSE_FO|61093) - pipe encoded
key1 = urllib.parse.quote('NSE_FO|61093', safe='')
status, n, sample = get('/historical-candle/' + key1 + '/5minute/2026-06-13/2026-06-13')
print('NIFTY FUT pipe-encoded:', status, 'n='+str(n), str(sample)[:80])
time.sleep(1.5)

# Test 2: NSE_INDEX Nifty 50 - 5min 1 day
key2 = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
status, n, sample = get('/historical-candle/' + key2 + '/5minute/2026-06-13/2026-06-13')
print('NSE_INDEX Nifty 50 5min:', status, 'n='+str(n), str(sample)[:80])
time.sleep(1.5)

# Test 3: expired-instruments NSE_INDEX 5min (Dec 2025)
status, n, sample = get('/expired-instruments/historical-candle/' + key2 + '/5minute/2025-12-30/2025-12-01')
print('expired NSE_INDEX 5min Dec 2025:', status, 'n='+str(n), str(sample)[:80])
time.sleep(1.5)

# Test 4: expired-instruments NSE_INDEX day (to confirm it works)
status, n, sample = get('/expired-instruments/historical-candle/' + key2 + '/day/2025-12-30/2025-12-01')
print('expired NSE_INDEX day Dec 2025:', status, 'n='+str(n), str(sample)[:80])
time.sleep(1.5)

# Test 5: yfinance availability
try:
    import yfinance as yf
    print('yfinance: available v' + yf.__version__)
    # Quick test - 5min data for last 5 days
    tk = yf.Ticker('^NSEI')
    hist = tk.history(period='5d', interval='5m')
    print('  yfinance NSEI 5m rows:', len(hist))
    if not hist.empty:
        print('  sample:', hist.iloc[-1][['Open','High','Low','Close']].to_dict())
except ImportError:
    print('yfinance: NOT installed')
except Exception as e:
    print('yfinance error:', str(e)[:100])
