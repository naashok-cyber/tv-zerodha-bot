"""
Validate all tokens in fut_tokens_clean.json against Upstox API.
For each entry, try the expiry date AND 2 trading days before (in case expiry = holiday).
Prints status: OK (with close price) or FAIL.
"""
import json, urllib.request, urllib.parse, time
from datetime import date, timedelta
from pathlib import Path

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
hdr = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': UA}

CLEAN = json.loads(Path('data/analysis/fut_tokens_clean.json').read_text())

def try_token(tok: int, exp: date, try_date: date) -> tuple[bool, float]:
    exp_dd = exp.strftime('%d-%m-%Y')
    inst_key = urllib.parse.quote(f'NSE_FO|{tok}|{exp_dd}', safe='')
    d = try_date.isoformat()
    url = f'{BASE}/expired-instruments/historical-candle/{inst_key}/day/{d}/{d}'
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            candles = (data.get('data') or {}).get('candles', [])
            if candles:
                return True, float(candles[0][4])
    except Exception:
        pass
    return False, 0.0


results = {}
print(f"{'Key':<30} {'Token':<8} {'Status':<6} {'Close':<10} {'Date Used'}")
print('-' * 70)

for key in sorted(CLEAN.keys()):
    und, exp_str = key.split('_', 1)
    tok = CLEAN[key]
    exp = date.fromisoformat(exp_str)

    ok = False
    close = 0.0
    used_date = exp
    # Try expiry date + 2 business days before
    for delta in range(0, 3):
        d = exp - timedelta(days=delta)
        if d.weekday() >= 5:  # skip weekends
            continue
        ok, close = try_token(tok, exp, d)
        time.sleep(0.8)
        if ok:
            used_date = d
            break

    status = 'OK' if ok else 'EMPTY'
    price_note = f'{close:.1f}' if ok else ''
    print(f"  {key:<30} {tok:<8} {status:<6} {price_note:<10} {used_date if ok else ''}")
    results[key] = {'token': tok, 'ok': ok, 'close': close, 'used_date': str(used_date)}

ok_count = sum(1 for v in results.values() if v['ok'])
print(f"\nTotal: {len(results)}, OK: {ok_count}, EMPTY: {len(results) - ok_count}")

# Save validation results
Path('data/analysis/fut_tokens_validated.json').write_text(
    json.dumps(results, indent=2)
)
