"""
Reconcile fut_tokens.json: match NSE bhavcopy dates to our target month list.
For each month (YYYY-MM), find the token from the map and record the ACTUAL expiry date.
Output: data/analysis/fut_tokens_clean.json  { "NIFTY_YYYY-MM-DD": token, ... }
"""
import json
from datetime import date
from pathlib import Path

RAW = Path('data/analysis/fut_tokens.json')
OUT = Path('data/analysis/fut_tokens_clean.json')

# Load raw token map (keyed by "SYM_YYYY-MM-DD")
raw = json.loads(RAW.read_text(encoding='utf-8-sig'))

# Build month → (actual_date, token) mapping
# Group by underlying + year-month, keep unique (deduplicate same token different date)
from collections import defaultdict

by_month = defaultdict(dict)   # {(und, 'YYYY-MM'): {actual_date: token}}
for key, tok in raw.items():
    parts = key.split('_', 1)
    if len(parts) != 2:
        continue
    und, date_str = parts
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        continue
    ym = d.strftime('%Y-%m')
    by_month[(und, ym)][d] = tok

# For each month, pick the date with the lowest date (most likely the actual expiry,
# since later dates could come from the next month's bhavcopy re-listing the prior month)
# BUT: if two different dates have the SAME token, they're the same contract — keep earliest
clean = {}
for (und, ym), date_token_map in sorted(by_month.items()):
    # Group by token value — same token = same contract, different date = data artifact
    token_to_dates = defaultdict(list)
    for d, tok in date_token_map.items():
        token_to_dates[tok].append(d)

    for tok, dates in token_to_dates.items():
        actual_date = min(dates)  # earliest date = most likely the real NSE expiry
        clean_key = f'{und}_{actual_date}'
        clean[clean_key] = tok

# Sort and save
clean_sorted = dict(sorted(clean.items()))
OUT.write_text(json.dumps(clean_sorted, indent=2))
print(f'Saved {len(clean_sorted)} entries to {OUT}')

# Print table
print(f"\n{'Underlying':<12} {'Actual Expiry':<14} {'Token':<8}")
print('-' * 40)
nifty_count = 0
bn_count = 0
for key, tok in clean_sorted.items():
    und, exp_str = key.split('_', 1)
    try:
        exp = date.fromisoformat(exp_str)
        key_upstox = f"NSE_FO|{tok}|{exp.strftime('%d-%m-%Y')}"
    except Exception:
        key_upstox = f"NSE_FO|{tok}|{exp_str}"
    print(f"  {und:<12} {exp_str:<14} {tok:<8}  {key_upstox}")
    if und == 'NIFTY':
        nifty_count += 1
    else:
        bn_count += 1

print(f'\nNIFTY: {nifty_count}, BANKNIFTY: {bn_count}')

# Show mismatches vs original lists
print('\n=== Date mismatches vs expected lists ===')
NIFTY_EXP = [
    date(2024, 10, 31), date(2024, 11, 28), date(2024, 12, 26),
    date(2025,  1, 30), date(2025,  2, 27), date(2025,  3, 27),
    date(2025,  4, 24), date(2025,  5, 29), date(2025,  6, 26),
    date(2025,  7, 31), date(2025,  8, 28), date(2025,  9, 25),
    date(2025, 10, 30), date(2025, 11, 27), date(2025, 12, 25),
    date(2026,  1, 29), date(2026,  2, 26), date(2026,  3, 27),
    date(2026,  4, 24), date(2026,  5, 29),
]
BN_EXP = [
    date(2024, 10, 30), date(2024, 11, 27), date(2024, 12, 25),
    date(2025,  1, 29), date(2025,  2, 26), date(2025,  3, 26),
    date(2025,  4, 23), date(2025,  5, 28), date(2025,  6, 25),
    date(2025,  7, 30), date(2025,  8, 27), date(2025,  9, 24),
    date(2025, 10, 29), date(2025, 11, 26), date(2025, 12, 24),
    date(2026,  1, 28), date(2026,  2, 25), date(2026,  3, 26),
    date(2026,  4, 23), date(2026,  5, 28),
]

for target_list, und in [(NIFTY_EXP, 'NIFTY'), (BN_EXP, 'BANKNIFTY')]:
    for exp in target_list:
        ym = exp.strftime('%Y-%m')
        # Find matching key in clean_sorted
        matching_keys = [(k, v) for k, v in clean_sorted.items()
                         if k.startswith(f'{und}_') and k[len(und)+1:len(und)+8] == ym]
        if not matching_keys:
            print(f'  {und} {exp}: NO TOKEN FOUND')
        elif len(matching_keys) == 1:
            actual_key, tok = matching_keys[0]
            actual_date = date.fromisoformat(actual_key.split('_', 1)[1])
            if actual_date != exp:
                print(f'  {und} {exp}: date mismatch → actual NSE={actual_date} token={tok}')
        else:
            print(f'  {und} {exp}: MULTIPLE MATCHES → {matching_keys}')
