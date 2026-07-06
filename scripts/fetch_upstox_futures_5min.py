"""
Download 5-min OHLCV for expired NIFTY and BANKNIFTY monthly futures contracts.

Token map: data/analysis/fut_tokens_clean.json  (built by extract_fut_tokens_from_nse.py)
Instrument key format: NSE_FO|{exchange_token}|{DD-MM-YYYY}
API: GET /v2/expired-instruments/historical-candle/{key}/5minute/{to}/{from}

Output:
  data/nifty_futures/{YYYY-MM}/{expiry}_5min.csv
  data/banknifty_futures/{YYYY-MM}/{expiry}_5min.csv
  data/nifty_futures/nifty_5min_futures.csv         (master)
  data/banknifty_futures/banknifty_5min_futures.csv (master)

Usage:
  python scripts/fetch_upstox_futures_5min.py [--underlying NIFTY|BANKNIFTY|ALL]
                                               [--expiry YYYY-MM-DD]
                                               [--force]
"""
import argparse, json, csv, time, urllib.request, urllib.parse
from datetime import date, timedelta
from pathlib import Path

api_token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
SLEEP = 1.2   # conservative rate limit for expired-instruments API

TOKEN_FILE = Path('data/analysis/fut_tokens_clean.json')

# Target months we care about (year-month strings)
NIFTY_MONTHS = {
    '2024-10', '2024-11', '2024-12',
    '2025-01', '2025-02', '2025-03', '2025-04', '2025-05', '2025-06',
    '2025-07', '2025-08', '2025-09', '2025-10', '2025-11', '2025-12',
    '2026-01', '2026-02', '2026-03', '2026-04', '2026-05',
}

BN_MONTHS = {
    '2024-10', '2024-11', '2024-12',
    '2025-01', '2025-02', '2025-03', '2025-04', '2025-05', '2025-06',
    '2025-07', '2025-08', '2025-09', '2025-10', '2025-11', '2025-12',
    '2026-01', '2026-02', '2026-03', '2026-04', '2026-05',
}

TODAY = date.today()


def api_candles(inst_key: str, interval: str,
                to_date: str, from_date: str) -> tuple[list, str | None]:
    url = (f'{BASE}/expired-instruments/historical-candle/'
           f'{urllib.parse.quote(inst_key, safe="")}/'
           f'{interval}/{to_date}/{from_date}')
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + api_token,
        'Accept': 'application/json', 'User-Agent': UA
    })
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            candles = (data.get('data') or {}).get('candles', [])
            return candles, None
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code}: {e.read().decode()[:150]}"


def candles_to_rows(candles: list, expiry: date, underlying: str) -> list[dict]:
    rows = []
    for c in candles:
        if len(c) < 6:
            continue
        ts, o, h, l, cl, vol = c[0], c[1], c[2], c[3], c[4], c[5]
        parts = ts.split('T')
        if len(parts) < 2:
            continue
        d = parts[0]
        t = parts[1][:5]
        rows.append({
            'date': d, 'time': t, 'open': o, 'high': h, 'low': l,
            'close': cl, 'volume': int(vol) if vol else 0,
            'expiry': expiry.isoformat(), 'underlying': underlying,
        })
    return rows


def save_csv(rows: list[dict], path: Path):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def combine_master(und_lower: str, out_dir: Path):
    master = out_dir / f'{und_lower}_5min_futures.csv'
    all_rows = []
    import re
    _ym_pattern = re.compile(r'^\d{4}-\d{2}$')
    for f in sorted(out_dir.rglob('*_5min.csv')):
        if f == master:
            continue
        # Only include files in YYYY-MM subdirectories (not old derived files in root)
        if not _ym_pattern.match(f.parent.name):
            continue
        with open(f, newline='') as fh:
            rows = list(csv.DictReader(fh))
            if rows and 'date' in rows[0] and 'time' in rows[0] and 'expiry' in rows[0]:
                all_rows.extend(rows)
    seen = set()
    deduped = []
    for r in all_rows:
        k = (r['date'], r['time'], r['expiry'])
        if k not in seen:
            seen.add(k)
            deduped.append(r)
    deduped.sort(key=lambda r: (r['expiry'], r['date'], r['time']))
    if deduped:
        # Canonical field order — use a fixed schema
        FIELDS = ['date', 'time', 'open', 'high', 'low', 'close', 'volume', 'expiry', 'underlying']
        extra = [k for k in deduped[0].keys() if k not in FIELDS]
        fieldnames = FIELDS + extra
        with open(master, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            w.writeheader()
            w.writerows(deduped)
        print(f"  Master: {master.name}  {len(deduped):,} rows")


def fetch_expiry(und: str, expiry: date, exch_token: int,
                 out_dir: Path, force: bool = False) -> int:
    out_file = out_dir / expiry.strftime('%Y-%m') / f'{expiry}_5min.csv'
    if out_file.exists() and not force:
        with open(out_file, newline='') as f:
            n = sum(1 for _ in csv.reader(f)) - 1
        print(f"  {und} {expiry}: cached ({n} rows)")
        return n

    exp_dd = expiry.strftime('%d-%m-%Y')
    inst_key = f'NSE_FO|{exch_token}|{exp_dd}'
    # Fetch from 30 trading days before expiry
    from_dt = expiry - timedelta(days=45)   # 45 calendar days ≈ 30 trading days
    print(f"  {und} {expiry}: fetching {from_dt} -> {expiry}  [token={exch_token}]")

    all_rows = []
    chunk_end = expiry
    while chunk_end >= from_dt:
        chunk_start = max(from_dt, chunk_end - timedelta(days=30))
        candles, err = api_candles(inst_key, '5minute',
                                   chunk_end.isoformat(), chunk_start.isoformat())
        time.sleep(SLEEP)
        if err:
            print(f"    [{chunk_start}..{chunk_end}] ERROR: {err[:100]}")
            break
        rows = candles_to_rows(candles, expiry, und)
        print(f"    [{chunk_start}..{chunk_end}]: {len(rows)} candles")
        all_rows.extend(rows)
        if chunk_start <= from_dt:
            break
        chunk_end = chunk_start - timedelta(days=1)

    seen = set()
    deduped = []
    for r in all_rows:
        k = (r['date'], r['time'])
        if k not in seen:
            seen.add(k)
            deduped.append(r)
    deduped.sort(key=lambda r: (r['date'], r['time']))

    save_csv(deduped, out_file)
    print(f"  => Saved {len(deduped)} rows  [{out_file.name}]")
    return len(deduped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--underlying', choices=['NIFTY', 'BANKNIFTY', 'ALL'], default='ALL')
    parser.add_argument('--expiry', help='Single expiry YYYY-MM-DD')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    if not TOKEN_FILE.exists():
        print(f"ERROR: {TOKEN_FILE} not found.")
        print("Run: python scripts/local/extract_fut_tokens_from_nse.py")
        print("Then: python scripts/local/reconcile_fut_tokens.py")
        return

    token_map = json.loads(TOKEN_FILE.read_text())
    print(f"Loaded {len(token_map)} tokens from {TOKEN_FILE}")

    jobs = []
    for key, tok in sorted(token_map.items()):
        und, exp_str = key.split('_', 1)
        try:
            exp = date.fromisoformat(exp_str)
        except Exception:
            continue

        # Filter by underlying
        if args.underlying != 'ALL' and und != args.underlying:
            continue

        # Only target months in our lists
        ym = exp.strftime('%Y-%m')
        target_months = NIFTY_MONTHS if und == 'NIFTY' else BN_MONTHS
        if ym not in target_months:
            continue

        # Only process expired contracts (can't use expired-instruments on active ones)
        if exp >= TODAY:
            print(f"  Skipping {und} {exp}: not yet expired (use regular endpoint for active contracts)")
            continue

        # Single expiry filter
        if args.expiry and exp_str != args.expiry:
            continue

        out_dir = Path(f'data/{und.lower()}_futures')
        jobs.append((und, exp, tok, out_dir))

    print(f"\nFetching {len(jobs)} expiries...\n")

    dirs_done = set()
    for i, (und, exp, tok, out_dir) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}]", end=' ')
        fetch_expiry(und, exp, tok, out_dir, force=args.force)
        dirs_done.add((und.lower(), out_dir))

    print("\nRebuilding master CSVs...")
    for und_lower, out_dir in dirs_done:
        combine_master(und_lower, out_dir)

    print("\nDone.")


if __name__ == '__main__':
    main()
