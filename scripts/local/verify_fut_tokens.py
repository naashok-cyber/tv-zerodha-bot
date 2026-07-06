"""Spot-check a few tokens from fut_tokens_clean.json against Upstox API."""
import json, urllib.request, urllib.parse, time
from datetime import date
from pathlib import Path

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
hdr = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': UA}

CLEAN = json.loads(Path('data/analysis/fut_tokens_clean.json').read_text())

def test(und_exp_key: str):
    und, exp_str = und_exp_key.split('_', 1)
    tok = CLEAN[und_exp_key]
    exp = date.fromisoformat(exp_str)
    exp_dd = exp.strftime('%d-%m-%Y')
    inst_key = f'NSE_FO|{tok}|{exp_dd}'
    url = (f'{BASE}/expired-instruments/historical-candle/'
           f'{urllib.parse.quote(inst_key, safe="")}/day/{exp_str}/{exp_str}')
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            candles = (data.get('data') or {}).get('candles', [])
            if candles:
                close = candles[0][4]
                print(f'  OK  {und_exp_key}: token={tok} key={inst_key} close={close}')
            else:
                print(f'  EMPTY  {und_exp_key}: token={tok}')
    except Exception as e:
        print(f'  FAIL  {und_exp_key}: {e}')
    time.sleep(1.1)

# Test a sample of early, mid, and recent months
tests = [
    'NIFTY_2024-10-31',    # OCT 2024 (earliest in list, token 35382)
    'NIFTY_2024-12-26',    # DEC 2024
    'NIFTY_2025-04-24',    # APR 2025 (known anchor)
    'NIFTY_2025-09-25',    # SEP 2025
    'NIFTY_2025-10-28',    # OCT 2025 (date mismatch from original list)
    'NIFTY_2025-12-30',    # DEC 2025 (holiday-shifted)
    'NIFTY_2026-05-26',    # MAY 2026 (most recent expired)
    'BANKNIFTY_2024-10-30',
    'BANKNIFTY_2025-04-24',
    'BANKNIFTY_2025-12-30',
]

for k in tests:
    if k in CLEAN:
        test(k)
    else:
        print(f'  MISSING from map: {k}')
