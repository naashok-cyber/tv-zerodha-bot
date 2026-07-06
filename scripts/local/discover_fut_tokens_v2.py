"""
Discover expired NIFTY and BANKNIFTY futures tokens.

Strategy:
1. Calibrate: get option token range for APR 2025 to find offset between
   futures token (54452, known) and options block.
2. For each other month: get option token range, target opt_min + offset ± 100
3. Fall back to wider search if not found.
4. Handles BANKNIFTY using BANKNIFTY APR 2025 anchor discovered during run.
5. Saves incrementally to resume if interrupted.
"""
import json, urllib.request, urllib.parse, time
from datetime import date
from pathlib import Path

token = open('.upstox_token').read().strip()
BASE = 'https://api.upstox.com/v2'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
SLEEP = 0.35
OUT_FILE = Path('data/analysis/fut_tokens.json')
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── known anchors (exchange_token from instruments master or confirmed via API) ──
KNOWN_TOKENS = {
    ('NIFTY',     date(2025, 4, 24)): 54452,
    # Current live (not expired yet — used for pattern reference only)
    # ('NIFTY', date(2026, 6, 26)): 62329,
    # ('NIFTY', date(2026, 7, 31)): 61093,
    # ('NIFTY', date(2026, 8, 28)): 58072,
}


def api_raw(path: str):
    url = BASE + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json', 'User-Agent': UA
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return {}, f"HTTP {e.code}: {e.read().decode()[:200]}"


def get_option_tokens(expiry_str: str, underlying_key: str) -> list[int]:
    """Return sorted list of exchange tokens for all option contracts for this expiry."""
    path = (f'/expired-instruments/option/contract'
            f'?instrument_key={urllib.parse.quote(underlying_key, safe="")}'
            f'&expiry_date={expiry_str}')
    data, err = api_raw(path)
    if err:
        print(f"    option/contract error: {err[:80]}")
        return []
    contracts = data.get('data', [])
    if not isinstance(contracts, list):
        return []
    tokens = []
    for c in contracts:
        key = c.get('instrument_key', '')
        parts = key.split('|')
        if len(parts) >= 2:
            try:
                tokens.append(int(parts[1]))
            except ValueError:
                pass
    return sorted(tokens)


def test_token(fut_tok: int, expiry: date) -> bool:
    """Return True if this token has candle data for the given expiry."""
    exp_str = expiry.strftime('%d-%m-%Y')
    test_date = expiry.isoformat()
    key = urllib.parse.quote(f'NSE_FO|{fut_tok}|{exp_str}', safe='')
    path = f'/expired-instruments/historical-candle/{key}/day/{test_date}/{test_date}'
    data, err = api_raw(path)
    time.sleep(SLEEP)
    if err:
        return False
    candles = (data.get('data') or {}).get('candles', [])
    return bool(candles)


def search_near(candidates: list[int], expiry: date, label: str) -> int | None:
    """Try each candidate token; return first that has candle data."""
    checked = set()
    for tok in candidates:
        if tok <= 0 or tok in checked:
            continue
        checked.add(tok)
        if test_token(tok, expiry):
            return tok
    return None


def find_token(expiry: date, opt_tokens: list[int],
               offset_hint: int | None, prev_token: int | None,
               underlying: str) -> int | None:
    """
    Layered search:
      Phase 1: targeted — opt_min + offset_hint ± 20  (if hint available)
      Phase 2: adjacent — opt_min-100..opt_min, opt_max..opt_max+100
      Phase 3: temporal — prev_token ± 200 step 1
      Phase 4: wide    — opt_min ± 500 step 10
    """
    if not opt_tokens:
        return None
    opt_min, opt_max = opt_tokens[0], opt_tokens[-1]
    n_opts = len(opt_tokens)
    print(f"    options: {n_opts} contracts, tokens [{opt_min}..{opt_max}]")

    # Phase 1: offset-guided
    if offset_hint is not None:
        target = opt_min + offset_hint
        cands = list(range(target - 25, target + 25))
        print(f"    Phase1: trying {target-25}..{target+25} (offset={offset_hint})")
        found = search_near(cands, expiry, "P1")
        if found:
            return found

    # Phase 2: adjacent to options block
    before = list(range(opt_min - 100, opt_min))
    after  = list(range(opt_max + 1, opt_max + 101))
    print(f"    Phase2: adjacent ±100 ({opt_min-100}..{opt_min}, {opt_max}..{opt_max+100})")
    for cand in (list(reversed(before)) + after):  # from closest outward
        if cand > 0 and test_token(cand, expiry):
            return cand

    # Phase 3: temporal (near previous found token)
    if prev_token:
        cands = []
        for d in range(1, 201):
            cands.extend([prev_token + d, prev_token - d])
        print(f"    Phase3: prev_token={prev_token} ±200")
        found = search_near(cands, expiry, "P3")
        if found:
            return found

    # Phase 4: wide search sampled
    cands = []
    for d in range(100, 1500, 10):
        cands.extend([opt_min - d, opt_min + d, opt_max + d])
    print(f"    Phase4: wide ±1500 sampled")
    return search_near(cands, expiry, "P4")


# ── expiry lists ────────────────────────────────────────────────────────────
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

NIFTY_IDX_KEY = 'NSE_INDEX|Nifty 50'
BN_IDX_KEY    = 'NSE_INDEX|Nifty Bank'


def load_results() -> dict:
    if OUT_FILE.exists():
        return json.loads(OUT_FILE.read_text())
    return {}


def save_results(results: dict):
    OUT_FILE.write_text(json.dumps(results, indent=2))


def calibrate_offset(underlying: str, anchor_exp: date, anchor_tok: int,
                     idx_key: str) -> int | None:
    """Fetch option tokens for the anchor month and return offset = anchor_tok - opt_min."""
    exp_str = anchor_exp.isoformat()
    print(f"  Calibrating offset for {underlying} anchor {anchor_exp} (token={anchor_tok})...")
    opt_tokens = get_option_tokens(exp_str, idx_key)
    time.sleep(1.0)
    if opt_tokens:
        offset = anchor_tok - opt_tokens[0]
        print(f"  Offset = {anchor_tok} - {opt_tokens[0]} = {offset}")
        return offset
    print("  Calibration failed: no option contracts")
    return None


def run_discovery(underlying: str, expiries: list[date], idx_key: str,
                  results: dict) -> int | None:
    """Discover tokens for one underlying. Returns the offset_hint for reuse."""
    offset_hint = None
    prev_token  = None

    # Seed from known tokens
    und_key = 'NIFTY' if underlying == 'NIFTY' else 'BANKNIFTY'
    for exp in sorted(expiries):
        key = f"{underlying}_{exp}"
        if (und_key, exp) in KNOWN_TOKENS:
            tok = KNOWN_TOKENS[(und_key, exp)]
            if key not in results:
                results[key] = tok
                save_results(results)
            prev_token = tok
            print(f"  {underlying} FUT {exp}: {tok} (known anchor)")

    # Pre-calibrate offset from known anchor
    if underlying == 'NIFTY' and ('NIFTY', date(2025, 4, 24)) in KNOWN_TOKENS:
        anchor_exp = date(2025, 4, 24)
        anchor_tok = KNOWN_TOKENS[('NIFTY', anchor_exp)]
        offset_hint = calibrate_offset(underlying, anchor_exp, anchor_tok, idx_key)
    elif underlying == 'BANKNIFTY':
        # Try to find a cached BANKNIFTY token to calibrate
        for exp in sorted(expiries):
            bkey = f"BANKNIFTY_{exp}"
            if bkey in results:
                anchor_exp = exp
                anchor_tok = results[bkey]
                offset_hint = calibrate_offset(underlying, anchor_exp, anchor_tok, idx_key)
                if offset_hint is not None:
                    prev_token = anchor_tok
                    break

    for exp in sorted(expiries):
        key = f"{underlying}_{exp}"
        if key in results:
            tok = results[key]
            print(f"  {underlying} FUT {exp}: {tok} (cached)")
            prev_token = tok
            continue

        print(f"\n  --- {underlying} FUT {exp} ---")

        # Get option token range
        exp_str = exp.isoformat()
        opt_tokens = get_option_tokens(exp_str, idx_key)
        time.sleep(1.0)

        if not opt_tokens:
            print(f"    No option contracts found — skipping")
            continue

        found = find_token(exp, opt_tokens, offset_hint, prev_token, underlying)

        if found:
            results[key] = found
            save_results(results)
            print(f"  => {underlying} FUT {exp}: token={found}  key=NSE_FO|{found}|{exp.strftime('%d-%m-%Y')}")

            # Compute/update offset
            if opt_tokens:
                new_offset = found - opt_tokens[0]
                print(f"     offset from opt_min: {new_offset}")
                if offset_hint is None:
                    offset_hint = new_offset
                else:
                    offset_hint = (offset_hint + new_offset) // 2  # smooth

            prev_token = found
        else:
            print(f"  => {underlying} FUT {exp}: NOT FOUND")

        time.sleep(0.5)

    return offset_hint


# ── MAIN ────────────────────────────────────────────────────────────────────
results = load_results()
print(f"Loaded {len(results)} cached results from {OUT_FILE}")

print("\n" + "=" * 60)
print("NIFTY monthly futures token discovery")
print("=" * 60)
run_discovery('NIFTY', NIFTY_EXPIRIES, NIFTY_IDX_KEY, results)

print("\n" + "=" * 60)
print("BANKNIFTY monthly futures token discovery")
print("=" * 60)
run_discovery('BANKNIFTY', BN_EXPIRIES, BN_IDX_KEY, results)

# Print final table
print("\n" + "=" * 60)
print("FINAL RESULTS")
print("=" * 60)
print(f"{'Underlying':<12} {'Expiry':<12} {'Token':<10} Key")
for k in sorted(results):
    tok = results[k]
    und, exp_str = k.rsplit('_', 1)
    exp = date.fromisoformat(exp_str)
    key = f"NSE_FO|{tok}|{exp.strftime('%d-%m-%Y')}"
    print(f"  {und:<12} {exp_str:<12} {tok:<10} {key}")

print(f"\nSaved to {OUT_FILE}")
