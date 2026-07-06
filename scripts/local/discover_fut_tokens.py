"""
Discover NIFTY and BANKNIFTY futures instrument tokens for each expired monthly expiry.

Strategy:
1. For each expired monthly expiry, fetch a batch of option contracts
2. Extract token range from option instrument keys (format: NSE_FO|{token}|DD-MM-YYYY)
3. Search nearby for the futures token by probing the expired-instruments API
4. Known anchor: NIFTY APR 2025 FUT = token 54452

Then build a complete token map and test all months.
"""
import json, urllib.request, urllib.parse, time, gzip, csv
from datetime import date, datetime

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def api(path, raw=False):
    url = BASE + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': UA
    })
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            if raw:
                return True, data
            candles = (data.get('data') or {}).get('candles', [])
            return True, candles
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:100]

def date_to_ddmmyyyy(d: date) -> str:
    return d.strftime('%d-%m-%Y')

def get_option_token_range(expiry_str: str, underlying_key: str) -> tuple[int, int]:
    """Get min/max exchange token from option contracts for this expiry."""
    ok, data = api(f'/expired-instruments/option/contract?instrument_key={urllib.parse.quote(underlying_key, safe="")}&expiry_date={expiry_str}', raw=True)
    if not ok:
        return 0, 0
    contracts = data.get('data', []) if isinstance(data.get('data'), list) else []
    tokens = []
    for c in contracts:
        key = c.get('instrument_key', '')
        parts = key.split('|')
        if len(parts) >= 2:
            try:
                tokens.append(int(parts[1]))
            except ValueError:
                pass
    return (min(tokens), max(tokens)) if tokens else (0, 0)

def test_fut_token(fut_token: int, expiry: date) -> bool:
    """Test if a futures token is valid for given expiry. Returns True if data found."""
    exp_str = date_to_ddmmyyyy(expiry)
    key = urllib.parse.quote(f'NSE_FO|{fut_token}|{exp_str}', safe='')
    # Fetch just 1 day near expiry to verify
    test_date = expiry.isoformat()
    ok, candles = api(f'/expired-instruments/historical-candle/{key}/day/{test_date}/{test_date}')
    time.sleep(0.4)
    return ok and isinstance(candles, list) and len(candles) > 0

def find_fut_token(expiry: date, opt_tok_min: int, opt_tok_max: int,
                   underlying: str, anchor_token: int | None = None) -> int | None:
    """
    Search for futures token near the option token range.
    Futures token is typically outside the option range (created separately by NSE).
    """
    mid = (opt_tok_min + opt_tok_max) // 2
    search_base = anchor_token if anchor_token else mid

    # Search strategy: try ±2000 from anchor in steps of 1
    # First do coarse search in steps of 100
    candidates = []
    for delta in range(0, 3000, 100):
        for sign in [1, -1]:
            candidates.append(search_base + sign * delta)

    print(f"  Searching for {underlying} FUT {expiry} (base={search_base})...")
    checked = set()
    for cand in candidates:
        if cand in checked or cand <= 0:
            continue
        checked.add(cand)
        if test_fut_token(cand, expiry):
            # Fine-tune: found around here, search ±10 to get exact
            for fine in range(cand - 10, cand + 10):
                if fine not in checked and test_fut_token(fine, expiry):
                    print(f"  FOUND: {underlying} FUT {expiry} = token {fine}")
                    return fine
            print(f"  FOUND (coarse): {underlying} FUT {expiry} ≈ token {cand}")
            return cand
    return None

# Monthly expiry dates from our backtest data
NIFTY_EXPIRIES = [
    date(2024, 10, 31), date(2024, 11, 28), date(2024, 12, 26),
    date(2025, 1, 30),  date(2025, 2, 27),  date(2025, 3, 27),
    date(2025, 4, 24),  date(2025, 5, 29),  date(2025, 6, 26),
    date(2025, 7, 31),  date(2025, 8, 28),  date(2025, 9, 25),
    date(2025, 10, 30), date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 29),  date(2026, 2, 26),  date(2026, 3, 27),
    date(2026, 4, 24),  date(2026, 5, 29),
]

BN_EXPIRIES = [
    date(2024, 10, 30), date(2024, 11, 27), date(2024, 12, 25),
    date(2025, 1, 29),  date(2025, 2, 26),  date(2025, 3, 26),
    date(2025, 4, 23),  date(2025, 5, 28),  date(2025, 6, 25),
    date(2025, 7, 30),  date(2025, 8, 27),  date(2025, 9, 24),
    date(2025, 10, 29), date(2025, 11, 26), date(2025, 12, 24),
    date(2026, 1, 28),  date(2026, 2, 25),  date(2026, 3, 26),
    date(2026, 4, 23),  date(2026, 5, 28),
]

# Step 1: Get option token range for each expiry to anchor search
print("=== STEP 1: Get option token ranges per expiry ===")
NIFTY_KEY   = 'NSE_INDEX|Nifty 50'
BN_KEY      = 'NSE_INDEX|Nifty Bank'

token_map = {}  # {(underlying, expiry): futures_token}

# Known anchors
KNOWN_TOKENS = {
    ('NIFTY', date(2025, 4, 24)): 54452,
    # Current from instruments master (not yet expired):
    # ('NIFTY', date(2026, 6, 30)): 62329,
    # ('NIFTY', date(2026, 7, 28)): 61093,
    # ('NIFTY', date(2026, 8, 25)): 58072,
}

# Step 2: For each expiry, get option token range then find futures token
print("\n=== STEP 2: Discover futures tokens ===")
all_results = {}

# Start with NIFTY
print("\n--- NIFTY ---")
prev_token = 54452  # known anchor for Apr 2025
for exp in sorted(NIFTY_EXPIRIES):
    if exp in [e for (u, e), t in KNOWN_TOKENS.items() if u == 'NIFTY']:
        tok = KNOWN_TOKENS[('NIFTY', exp)]
        print(f"  NIFTY FUT {exp}: token={tok} (known)")
        all_results[('NIFTY', exp)] = tok
        prev_token = tok
        continue

    # Get option token range as anchor
    exp_str = exp.isoformat()
    tok_min, tok_max = get_option_token_range(exp_str, NIFTY_KEY)
    time.sleep(1.0)

    if tok_min and tok_max:
        print(f"  NIFTY {exp}: option tokens {tok_min}-{tok_max}, searching futures...")
        # Futures token is often just below the minimum option token for that expiry
        # Try around the min option token first, then around prev_token
        found = None
        for search_base in [tok_min - 500, prev_token]:
            for delta in range(0, 2000, 50):
                for sign in [1, -1]:
                    cand = search_base + sign * delta
                    if cand <= 0:
                        continue
                    if test_fut_token(cand, exp):
                        found = cand
                        break
                if found:
                    break
            if found:
                break
        if found:
            all_results[('NIFTY', exp)] = found
            prev_token = found
            print(f"  => NIFTY FUT {exp}: token={found}")
        else:
            print(f"  => NIFTY FUT {exp}: NOT FOUND")
    else:
        print(f"  NIFTY {exp}: no option contracts (skipping)")
    time.sleep(0.5)

# Save results
print("\n=== RESULTS ===")
print(f"{'Underlying':<12} {'Expiry':<12} {'Token':<10} {'Key'}")
for (und, exp), tok in sorted(all_results.items()):
    key = f"NSE_FO|{tok}|{date_to_ddmmyyyy(exp)}"
    print(f"  {und:<12} {str(exp):<12} {tok:<10} {key}")

# Save to JSON for use in fetch script
import json as j
out = {f"{und}_{exp}": tok for (und, exp), tok in all_results.items()}
with open('data/analysis/fut_tokens.json', 'w') as f:
    j.dump(out, f, indent=2)
print(f"\nSaved to data/analysis/fut_tokens.json")
