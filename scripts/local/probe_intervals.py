"""Final clean probe - intervals for current NIFTY FUT + expired options key lookup."""
import json, urllib.request, urllib.parse, time

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def api(path):
    url = BASE + path
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': UA})
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            candles = data.get('data', {}).get('candles', [])
            return 'OK', len(candles), candles[:1]
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:250]
        try:
            msg = json.loads(body)['errors'][0]['message']
        except Exception:
            msg = body[:80]
        return str(e.code), 0, msg

key_fut = urllib.parse.quote('NSE_FO|61093', safe='')

print("=== Current NIFTY FUT (NSE_FO|61093) - regular historical-candle ===")
for interval in ['1minute', '3minute', '5minute', '10minute', '15minute', '30minute', '60minute', 'day']:
    status, n, info = api(f'/historical-candle/{key_fut}/{interval}/2026-06-13/2026-06-12')
    print(f"  {interval:<12}: {status}  n={n}  {str(info)[:80]}")
    time.sleep(1.1)

print("\n=== expired-instruments error message for NSE_INDEX ===")
key_idx = urllib.parse.quote('NSE_INDEX|Nifty 50', safe='')
status, n, info = api(f'/expired-instruments/historical-candle/{key_idx}/5minute/2025-12-26/2025-12-24')
print(f"  Status={status}  msg={info[:150]}")
time.sleep(1.1)

print("\n=== NIFTY option contracts for Dec 2025 (correct expiry date) ===")
# Dec 2025 monthly expiry for NIFTY was 2025-12-25 (last Thursday)
for exp_date in ['2025-12-25', '2025-12-26', '2025-12-24']:
    status, n, info = api(f'/expired-instruments/option/contract?instrument_key=NSE_INDEX%7CNifty+50&expiry_date={exp_date}')
    print(f"  expiry={exp_date}: status={status} n={n}")
    if n > 0:
        print(f"  Sample key: {info}")
    time.sleep(1.1)

print("\n=== Can we use expired option key to access futures candles? ===")
# Get a working option contract key to understand format
status2, n2, sample2 = api('/expired-instruments/option/contract?instrument_key=NSE_INDEX%7CNifty+50&expiry_date=2026-01-27')
print(f"  Jan 2026 contracts: status={status2} n={n2}")
if n2 > 0 and isinstance(sample2, list):
    print(f"  Key format: {sample2[0] if sample2 else 'N/A'}")
time.sleep(1.1)

# Also check if we can get futures via the option chain (futures index key)
print("\n=== Check Upstox instruments master for NIFTY FUT history approach ===")
# The correct approach for expired futures is: find the token from when the contract was active
# Upstox instruments endpoint (live)
status3, n3, info3 = api('/instruments?exchange=NSE_FO&segment=FUTIDX')
print(f"  /instruments?exchange=NSE_FO&segment=FUTIDX: status={status3}  {str(info3)[:100]}")
