"""Rebuild master 5-min futures CSVs from all cached per-month files."""
import csv, re
from pathlib import Path

FIELDS = ['date', 'time', 'open', 'high', 'low', 'close', 'volume', 'expiry', 'underlying']
_ym_pattern = re.compile(r'^\d{4}-\d{2}$')


def combine_master(und_lower: str, out_dir: Path):
    master = out_dir / f'{und_lower}_5min_futures.csv'
    all_rows = []
    for f in sorted(out_dir.rglob('*_5min.csv')):
        if f == master:
            continue
        if not _ym_pattern.match(f.parent.name):
            continue
        with open(f, newline='', encoding='utf-8') as fh:
            rows = list(csv.DictReader(fh))
            if rows and 'date' in rows[0] and 'close' in rows[0]:
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
        extra = [k for k in deduped[0].keys() if k not in FIELDS]
        fieldnames = FIELDS + extra
        with open(master, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            w.writeheader()
            w.writerows(deduped)
        print(f"  {master}: {len(deduped):,} rows ({len(set(r['expiry'] for r in deduped))} expiries)")

        # Count by expiry
        from collections import Counter
        counts = Counter(r['expiry'] for r in deduped)
        for exp in sorted(counts):
            print(f"    {exp}: {counts[exp]:5d} rows")
    else:
        print(f"  {und_lower}: no data")


print("=== NIFTY ===")
combine_master('nifty', Path('data/nifty_futures'))

print("\n=== BANKNIFTY ===")
combine_master('banknifty', Path('data/banknifty_futures'))
