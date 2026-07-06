"""
Fix BANKNIFTY JAN/FEB 2025 expiry dates in fut_tokens_clean.json.
NSE moved BANKNIFTY monthly expiry from Wednesday to Thursday (SEBI alignment ~2025).
Jan 15 2025 bhavcopy confirms:
  BANKNIFTY25JANFUT: expiry=2025-01-30 (Thu), token=35012
  BANKNIFTY25FEBFUT: expiry=2025-02-27 (Thu), token=35117

Also test corrected BN DEC 2024 date.
"""
import json, urllib.request, urllib.parse, time
from datetime import date
from pathlib import Path

api_token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
hdr = {'Authorization': 'Bearer ' + api_token, 'Accept': 'application/json', 'User-Agent': UA}

CLEAN_FILE = Path('data/analysis/fut_tokens_clean.json')
clean = json.loads(CLEAN_FILE.read_text())


def test(tok: int, exp_str: str, test_date_str: str) -> tuple[bool, float]:
    exp = date.fromisoformat(exp_str)
    exp_dd = exp.strftime('%d-%m-%Y')
    key = urllib.parse.quote(f'NSE_FO|{tok}|{exp_dd}', safe='')
    url = f'{BASE}/expired-instruments/historical-candle/{key}/day/{test_date_str}/{test_date_str}'
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

# Test corrected dates
corrections = [
    # (old_key, new_key, token, test_date)
    ('BANKNIFTY_2025-01-29', 'BANKNIFTY_2025-01-30', 35012, '2025-01-30'),
    ('BANKNIFTY_2025-02-26', 'BANKNIFTY_2025-02-27', 35117, '2025-02-27'),
]

changes_made = False
for old_key, new_key, tok, test_date in corrections:
    ok, close = test(tok, new_key.split('_', 1)[1], test_date)
    time.sleep(1.0)
    status = f'OK close={close:.1f}' if ok else 'STILL EMPTY'
    print(f"  {new_key} (token={tok}): {status}")
    if ok:
        # Apply correction
        if old_key in clean:
            del clean[old_key]
        clean[new_key] = tok
        changes_made = True
        print(f"    Updated: {old_key} -> {new_key}")

if changes_made:
    CLEAN_FILE.write_text(json.dumps(dict(sorted(clean.items())), indent=2))
    print(f"\nSaved corrected token map ({len(clean)} entries)")
else:
    print("\nNo changes made")
