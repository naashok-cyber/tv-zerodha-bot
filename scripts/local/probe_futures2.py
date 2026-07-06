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

# Load instruments
req = urllib.request.Request(
    'https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz',
    headers={'User-Agent': UA}
)
with urllib.request.urlopen(req, timeout=30) as r:
    instruments = json.loads(gzip.decompress(r.read()))

print(f"Total: {len(instruments)} instruments")

# Check structure of first instrument
print("\nSample instrument keys:", list(instruments[0].keys()))
print("Sample:", instruments[0])

# Find types in NSE_FO segment
import collections
types = collections.Counter()
segments = collections.Counter()
for i in instruments:
    types[i.get('instrument_type', i.get('type', 'unknown'))] += 1
    segments[i.get('exchange_segment', i.get('segment', i.get('exchange', 'unknown')))] += 1

print("\nInstrument types (top 10):", types.most_common(10))
print("Segments (top 10):", segments.most_common(10))

# Try to find NIFTY futures by searching various fields
nifty_fut = []
for i in instruments:
    ts = str(i.get('tradingsymbol', i.get('trading_symbol', ''))).upper()
    seg = str(i.get('exchange_segment', i.get('segment', i.get('exchange', '')))).upper()
    itype = str(i.get('instrument_type', i.get('type', ''))).upper()
    if 'NIFTYFUT' in ts or ('NIFTY' in ts and 'FUT' in ts and 'BANK' not in ts):
        nifty_fut.append(i)

print(f"\nNIFTY futures found: {len(nifty_fut)}")
for f in nifty_fut[:5]:
    print(f"  {f}")
