"""
Investigate and fix the 6 EMPTY (non-current) tokens.
Tests with dates 1-30 days before expiry to see if data exists at all.
Also re-extracts from NSE bhavcopy using an earlier date (month before expiry).
"""
import json, urllib.request, urllib.parse, zipfile, io, csv, time
from datetime import date, timedelta
from pathlib import Path

api_token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
hdr = {'Authorization': 'Bearer ' + api_token, 'Accept': 'application/json', 'User-Agent': UA}

CLEAN = json.loads(Path('data/analysis/fut_tokens_clean.json').read_text())

# Failed non-current months
FAILS = {
    'NIFTY_2024-12-26': 35005,
    'BANKNIFTY_2025-01-29': 35012,
    'BANKNIFTY_2025-02-26': 35117,
    'BANKNIFTY_2025-03-27': 58958,
    'BANKNIFTY_2025-09-25': 52995,
    'NIFTY_2025-09-25': 53001,
}


def test_token_on_date(tok: int, exp: date, try_date: date) -> tuple[bool, float]:
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


def get_bhavcopy_idf(date_str: str) -> dict:
    url = f'https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip'
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        z = zipfile.ZipFile(io.BytesIO(data))
        content = z.read(z.namelist()[0]).decode('utf-8', errors='replace')
        result = {}
        reader = csv.DictReader(content.splitlines())
        for row in reader:
            if row.get('FinInstrmTp') == 'IDF' and row.get('TckrSymb') in ('NIFTY', 'BANKNIFTY'):
                sym = row['TckrSymb']
                expiry = row.get('XpryDt', '')
                tok_str = row.get('FinInstrmId', '').strip()
                name = row.get('FinInstrmNm', '')
                close = row.get('ClsPric', '')
                if tok_str.isdigit():
                    result[(sym, expiry)] = (int(tok_str), name, close)
        return result
    except Exception as e:
        return {}


print("=== Testing failed tokens at earlier dates ===\n")

for key, tok in FAILS.items():
    und, exp_str = key.split('_', 1)
    exp = date.fromisoformat(exp_str)
    print(f"\n--- {key} (token={tok}) ---")

    # Try progressively earlier dates
    for delta in [0, 1, 2, 7, 14, 21, 28]:
        d = exp - timedelta(days=delta)
        if d.weekday() >= 5:
            continue
        ok, close = test_token_on_date(tok, exp, d)
        time.sleep(0.8)
        if ok:
            print(f"  OK at {d}: close={close:.1f}  -> USE THIS DATE")
            break
        print(f"  EMPTY at {d}")
    else:
        print(f"  All dates empty — checking bhavcopy for correct token")

    # Re-check NSE bhavcopy to confirm token
    # Try the month before expiry to find the contract when it was mid-month
    mid_date = (exp.replace(day=1) - timedelta(days=15)).replace(day=15)  # ~mid previous month
    date_str = mid_date.strftime('%Y%m%d')
    print(f"  Checking bhavcopy around {mid_date}...")
    bhav = get_bhavcopy_idf(date_str)
    time.sleep(0.8)
    for (sym, exp_bhav), (t, name, close) in bhav.items():
        if sym == und:
            print(f"    bhavcopy {date_str}: {sym} {name} expiry={exp_bhav} token={t} close={close}")

print("\nDone.")
