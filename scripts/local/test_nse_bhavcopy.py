"""Verify NSE bhavcopy FinInstrmId matches Upstox exchange_token."""
import urllib.request, zipfile, io, csv, time

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

def get_index_futs(date_str):
    url = f'https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip'
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
    except Exception as e:
        print(f'  FAIL {date_str}: {e}')
        return {}
    z = zipfile.ZipFile(io.BytesIO(data))
    content = z.read(z.namelist()[0]).decode('utf-8', errors='replace')
    reader = csv.DictReader(content.splitlines())
    tokens = {}
    for row in reader:
        if row.get('FinInstrmTp') == 'IDF' and row.get('TckrSymb') in ('NIFTY', 'BANKNIFTY'):
            sym = row['TckrSymb']
            expiry = row.get('XpryDt','')
            tok = row.get('FinInstrmId','').strip()
            close = row.get('ClsPric','')
            name = row.get('FinInstrmNm','')
            print(f'  {name}: NSE_token={tok} expiry={expiry} close={close}')
            tokens[(sym, expiry)] = tok
    return tokens

# APR 24, 2025 is the expiry date — bhavcopy on that day
print('=== APR 24, 2025 bhavcopy (NIFTY APR 2025 FUT expiry) ===')
print('Upstox known: NIFTY APR 2025 FUT = exchange_token 54452')
t = get_index_futs('20250424')
print(f'NSE tokens: {t}')

time.sleep(1)

# Also check a day before expiry to make sure the FUT is still listed
print('\n=== APR 23, 2025 bhavcopy ===')
t2 = get_index_futs('20250423')
print(f'NSE tokens: {t2}')
