"""
Extract NIFTY and BANKNIFTY monthly futures exchange tokens from NSE FO Bhavcopy.

NSE FO bhavcopy FinInstrmId == Upstox exchange_token (confirmed: NIFTY APR 2025 = 54452).
Downloads one bhavcopy per month (expiry date) to extract the IDF (Index Futures) token.

Output: data/analysis/fut_tokens.json
"""
import urllib.request, zipfile, io, csv, json, time
from datetime import date, timedelta
from pathlib import Path

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
OUT = Path('data/analysis/fut_tokens.json')
OUT.parent.mkdir(parents=True, exist_ok=True)
SLEEP = 0.8  # polite rate limiting for NSE archives

NIFTY_EXPIRIES = [
    date(2024, 10, 31), date(2024, 11, 28), date(2024, 12, 26),
    date(2025,  1, 30), date(2025,  2, 27), date(2025,  3, 27),
    date(2025,  4, 24), date(2025,  5, 29), date(2025,  6, 26),
    date(2025,  7, 31), date(2025,  8, 28), date(2025,  9, 25),
    date(2025, 10, 30), date(2025, 11, 27), date(2025, 12, 25),
    date(2026,  1, 29), date(2026,  2, 26), date(2026,  3, 27),
    date(2026,  4, 24), date(2026,  5, 29),
]

BN_EXPIRIES = [
    date(2024, 10, 30), date(2024, 11, 27), date(2024, 12, 25),
    date(2025,  1, 29), date(2025,  2, 26), date(2025,  3, 26),
    date(2025,  4, 23), date(2025,  5, 28), date(2025,  6, 25),
    date(2025,  7, 30), date(2025,  8, 27), date(2025,  9, 24),
    date(2025, 10, 29), date(2025, 11, 26), date(2025, 12, 24),
    date(2026,  1, 28), date(2026,  2, 25), date(2026,  3, 26),
    date(2026,  4, 23), date(2026,  5, 28),
]


def fetch_bhavcopy(date_str: str) -> dict | None:
    """Download bhavcopy for given date (YYYYMMDD). Returns {(sym, expiry_iso): token_int}."""
    url = f'https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip'
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # holiday or weekend
        print(f'  HTTP {e.code} for {date_str}')
        return None
    except Exception as e:
        print(f'  Error fetching {date_str}: {e}')
        return None

    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        content = z.read(z.namelist()[0]).decode('utf-8', errors='replace')
    except Exception as e:
        print(f'  ZIP error {date_str}: {e}')
        return None

    result = {}
    reader = csv.DictReader(content.splitlines())
    for row in reader:
        if row.get('FinInstrmTp') != 'IDF':
            continue
        sym = row.get('TckrSymb', '')
        if sym not in ('NIFTY', 'BANKNIFTY'):
            continue
        expiry = row.get('XpryDt', '')
        tok_str = row.get('FinInstrmId', '').strip()
        if expiry and tok_str.isdigit():
            result[(sym, expiry)] = int(tok_str)
    return result


def get_token_for_expiry(expiry: date, sym: str, expiries_needed: list[date],
                          all_tokens: dict) -> int | None:
    """
    Try expiry date and a few days before (in case of holiday/weekend).
    Also look in the bhavcopy data for the date 3 months before the expiry
    to find the contract when it was the 'far month'.
    """
    # Try expiry date and up to 4 business days before
    for delta in range(0, 5):
        try_date = expiry - timedelta(days=delta)
        if try_date.weekday() >= 5:  # skip weekends
            continue
        date_str = try_date.strftime('%Y%m%d')
        bhav = fetch_bhavcopy(date_str)
        time.sleep(SLEEP)
        if bhav is None:
            continue
        expiry_iso = expiry.isoformat()
        if (sym, expiry_iso) in bhav:
            tok = bhav[(sym, expiry_iso)]
            print(f'  Found {sym} {expiry_iso}: token={tok} (from bhavcopy {date_str})')
            # Also record any other tokens found in this bhavcopy
            for (s, e), t in bhav.items():
                key = f'{s}_{e}'
                if key not in all_tokens:
                    all_tokens[key] = t
            return tok
        # Not found on this date but bhavcopy downloaded OK - record all IDF tokens
        for (s, e), t in bhav.items():
            key = f'{s}_{e}'
            if key not in all_tokens:
                all_tokens[key] = t
    return None


def main():
    # Load existing results
    all_tokens = {}
    if OUT.exists():
        try:
            all_tokens = json.loads(OUT.read_text(encoding='utf-8-sig'))
            print(f'Loaded {len(all_tokens)} existing tokens')
        except Exception:
            all_tokens = {}

    print('\n=== Processing NIFTY expiries ===')
    for exp in sorted(NIFTY_EXPIRIES):
        key = f'NIFTY_{exp}'
        if key in all_tokens:
            print(f'  NIFTY {exp}: {all_tokens[key]} (cached)')
            continue
        tok = get_token_for_expiry(exp, 'NIFTY', NIFTY_EXPIRIES, all_tokens)
        if tok:
            all_tokens[key] = tok
        else:
            print(f'  NIFTY {exp}: NOT FOUND in bhavcopy')
        # Save incrementally
        OUT.write_text(json.dumps(all_tokens, indent=2, sort_keys=True))

    print('\n=== Processing BANKNIFTY expiries ===')
    for exp in sorted(BN_EXPIRIES):
        key = f'BANKNIFTY_{exp}'
        if key in all_tokens:
            print(f'  BANKNIFTY {exp}: {all_tokens[key]} (cached)')
            continue
        tok = get_token_for_expiry(exp, 'BANKNIFTY', BN_EXPIRIES, all_tokens)
        if tok:
            all_tokens[key] = tok
        else:
            print(f'  BANKNIFTY {exp}: NOT FOUND in bhavcopy')
        OUT.write_text(json.dumps(all_tokens, indent=2, sort_keys=True))

    print('\n=== FINAL TOKEN MAP ===')
    for k in sorted(all_tokens):
        tok = all_tokens[k]
        parts = k.split('_', 1)
        und, exp_str = parts[0], parts[1]
        try:
            exp = date.fromisoformat(exp_str)
            key_str = f"NSE_FO|{tok}|{exp.strftime('%d-%m-%Y')}"
        except Exception:
            key_str = f"NSE_FO|{tok}|{exp_str}"
        print(f'  {und:<12} {exp_str:<12} {tok:<8} {key_str}')

    print(f'\nSaved {len(all_tokens)} tokens to {OUT}')


if __name__ == '__main__':
    main()
