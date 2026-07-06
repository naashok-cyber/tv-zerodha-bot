#!/usr/bin/env python3
"""
Fetch historical 5-min options data from Upstox Expired Instruments API (Plus plan).

Requires Upstox Plus subscription - provides OHLCV + OI data for expired option
contracts at 1min / 3min / 5min / 15min / 30min / day intervals.

NSE FNO (NIFTY, BANKNIFTY, MIDCPNIFTY): supported, data back to Oct 2024+
MCX (NATURALGAS, CRUDEOILM): NOT yet supported in Upstox Expired Instruments API

── Credentials (.env.upstox in project root) ────────────────────────────────
  UPSTOX_API_KEY      - client_id from Upstox Developer Portal -> My Apps
  UPSTOX_API_SECRET   - client_secret
  UPSTOX_REDIRECT_URI - http://localhost:8000/callback

── One-time login (token expires daily) ─────────────────────────────────────
  python scripts/fetch_upstox_options.py --login

── List available expired expiries for an underlying ─────────────────────────
  python scripts/fetch_upstox_options.py --expiries NIFTY
  python scripts/fetch_upstox_options.py --expiries BANKNIFTY

── Fetch complete options chain for an expired expiry ────────────────────────
  python scripts/fetch_upstox_options.py NIFTY 2026-05-29
  python scripts/fetch_upstox_options.py BANKNIFTY 2026-05-28
  python scripts/fetch_upstox_options.py NIFTY 2026-05-29 --interval 1minute

── Fetch ALL available expiries for an underlying ────────────────────────────
  python scripts/fetch_upstox_options.py NIFTY --all-expiries
  python scripts/fetch_upstox_options.py BANKNIFTY --all-expiries

Saves to: data/{underlying}_options/{YYYY-MM}/{SYMBOL}_{interval}.csv
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

# ── Constants ──────────────────────────────────────────────────────────────────

BASE_URL   = "https://api.upstox.com/v2"
TOKEN_FILE = Path(".upstox_token")
FETCH_DELAY = 1.1   # ~0.9 req/sec (Upstox expired API rate limit)

# Default strike ranges to keep data lean (only strikes near historical price range)
STRIKE_RANGES: dict[str, tuple[int, int]] = {
    "NIFTY":      (21000, 26500),
    "BANKNIFTY":  (45000, 62000),
    "MIDCPNIFTY": (8000,  12000),
    "FINNIFTY":   (18000, 26000),
}

# Monthly expiry weekday per underlying (0=Mon … 6=Sun)
MONTHLY_EXPIRY_DOW: dict[str, int] = {
    "NIFTY":      3,  # last Thursday
    "BANKNIFTY":  2,  # last Wednesday
    "MIDCPNIFTY": 1,  # last Tuesday
    "FINNIFTY":   3,  # last Thursday
}

# Underlying key mapping for Upstox Expired Instruments API
UNDERLYING_KEYS: dict[str, str] = {
    "NIFTY":      "NSE_INDEX|Nifty 50",
    "BANKNIFTY":  "NSE_INDEX|Nifty Bank",
    "MIDCPNIFTY": "NSE_INDEX|Nifty MidCap Select",
    "FINNIFTY":   "NSE_INDEX|Nifty Fin Service",
    "SENSEX":     "BSE_INDEX|SENSEX",
}

OUT_DIRS: dict[str, str] = {
    "NIFTY":      "data/nifty_options",
    "BANKNIFTY":  "data/banknifty_options",
    "MIDCPNIFTY": "data/midcpnifty_options",
    "FINNIFTY":   "data/finnifty_options",
}

FIELDS = ["date", "time", "open", "high", "low", "close", "volume", "oi",
          "expiry", "strike", "option_type"]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


# ── Auth ───────────────────────────────────────────────────────────────────────

def _env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        env_file = Path(".env.upstox")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith(key + "="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not val:
        print(f"ERROR: {key} not set in environment or .env.upstox")
        sys.exit(1)
    return val


def _load_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    print("No access token. Run:  python scripts/fetch_upstox_options.py --login")
    sys.exit(1)


def _exchange_code(code: str) -> str:
    payload = urllib.parse.urlencode({
        "code":          code,
        "client_id":     _env("UPSTOX_API_KEY"),
        "client_secret": _env("UPSTOX_API_SECRET"),
        "redirect_uri":  _env("UPSTOX_REDIRECT_URI"),
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/login/authorization/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json", "User-Agent": _UA},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    token = data.get("access_token", "")
    if not token:
        print("Token exchange failed:", data)
        sys.exit(1)
    return token


def do_login() -> None:
    api_key      = _env("UPSTOX_API_KEY")
    redirect_uri = _env("UPSTOX_REDIRECT_URI")
    auth_url = (
        "https://api.upstox.com/v2/login/authorization/dialog?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id":     api_key,
            "redirect_uri":  redirect_uri,
            "state":         "upstox_login",
        })
    )
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname in ("localhost", "127.0.0.1"):
        port = parsed.port or 8000
        code_holder: dict = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code_holder["code"] = params.get("code", [""])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Login successful. You can close this tab.")
            def log_message(self, *args): pass

        server = HTTPServer(("", port), _Handler)
        t = Thread(target=server.handle_request, daemon=True)
        t.start()
        print(f"\nOpen in browser:\n\n  {auth_url}\n")
        print(f"Waiting on localhost:{port} ...")
        t.join(timeout=120)
        server.server_close()
        code = code_holder.get("code", "")
        if not code:
            print("Timed out. Paste auth code from redirect URL:")
            code = input("Auth code: ").strip()
    else:
        print(f"\nOpen in browser:\n\n  {auth_url}\n")
        code = input("Paste auth code: ").strip()

    TOKEN_FILE.write_text(_exchange_code(code))
    print(f"Token saved to {TOKEN_FILE}")


# ── API helpers ────────────────────────────────────────────────────────────────

def _api_get(endpoint: str, token: str, params: dict | None = None,
             _retries: int = 5) -> dict:
    url = f"{BASE_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json", "User-Agent": _UA},
    )
    for attempt in range(_retries):
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                wait = 2 ** (attempt + 2)  # 4s, 8s, 16s, 32s, 64s
                print(f"      429 rate limit - waiting {wait}s (attempt {attempt+1}/{_retries})")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body[:400]}") from e
    raise RuntimeError(f"Exceeded {_retries} retries due to rate limiting")


def get_expiries(underlying: str, token: str) -> list[str]:
    uk = UNDERLYING_KEYS.get(underlying.upper())
    if not uk:
        print(f"Unknown underlying '{underlying}'. Supported: {', '.join(UNDERLYING_KEYS)}")
        sys.exit(1)
    r = _api_get("/expired-instruments/expiries", token,
                 params={"instrument_key": uk})
    return sorted(r.get("data", []))


def get_option_contracts(underlying: str, expiry: date, token: str) -> list[dict]:
    uk = UNDERLYING_KEYS[underlying.upper()]
    r = _api_get("/expired-instruments/option/contract", token,
                 params={"instrument_key": uk,
                         "expiry_date": expiry.isoformat()})
    return r.get("data", [])


def fetch_candles(expired_key: str, interval: str,
                  from_date: date, to_date: date, token: str) -> list[dict]:
    """Fetch OHLCV candles. Upstox returns up to ~100 days per request for 5min."""
    encoded = urllib.parse.quote(expired_key, safe="")
    endpoint = (
        f"/expired-instruments/historical-candle/{encoded}/{interval}"
        f"/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    try:
        r = _api_get(endpoint, token)
        return r.get("data", {}).get("candles", [])
    except Exception as exc:
        print(f"      ERR {expired_key}: {exc}")
        return []


# ── CSV ────────────────────────────────────────────────────────────────────────

def _parse_ts(ts: str) -> tuple[str, str]:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")
    except Exception:
        return ts[:10], ts[11:19]


def write_csv(path: Path, candles: list, expiry: date,
              strike: float, opt_type: str) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for c in candles:
            d, t = _parse_ts(c[0])
            w.writerow({"date": d, "time": t,
                        "open": c[1], "high": c[2], "low": c[3], "close": c[4],
                        "volume": c[5] if len(c) > 5 else "",
                        "oi":     c[6] if len(c) > 6 else "",
                        "expiry": str(expiry), "strike": strike,
                        "option_type": opt_type})


def append_missing(path: Path, candles: list, expiry: date,
                   strike: float, opt_type: str) -> int:
    existing: set[str] = set()
    with path.open("r") as f:
        for row in csv.DictReader(f):
            existing.add(f"{row['date']} {row['time']}")
    new_rows = []
    for c in candles:
        d, t = _parse_ts(c[0])
        if f"{d} {t}" not in existing:
            new_rows.append({"date": d, "time": t,
                             "open": c[1], "high": c[2], "low": c[3], "close": c[4],
                             "volume": c[5] if len(c) > 5 else "",
                             "oi":     c[6] if len(c) > 6 else "",
                             "expiry": str(expiry), "strike": strike,
                             "option_type": opt_type})
    if new_rows:
        with path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerows(new_rows)
    return len(new_rows)


# ── Main fetch ─────────────────────────────────────────────────────────────────

def fetch_expiry_options(underlying: str, expiry: date,
                         interval: str = "5minute",
                         min_strike: int | None = None,
                         max_strike: int | None = None) -> int:
    token    = _load_token()
    underlying = underlying.upper()
    out_dir  = Path(OUT_DIRS.get(underlying, f"data/{underlying.lower()}_options"))
    exp_dir  = out_dir / f"{expiry.year}-{expiry.month:02d}"
    exp_dir.mkdir(parents=True, exist_ok=True)

    # Apply default strike range if not overridden
    if min_strike is None and max_strike is None:
        rng = STRIKE_RANGES.get(underlying)
        if rng:
            min_strike, max_strike = rng

    print(f"\n{'='*65}")
    print(f"  {underlying} {expiry} - Upstox Plus fetch ({interval})")
    if min_strike or max_strike:
        print(f"  Strike filter: {min_strike or 0} - {max_strike or 'inf'}")
    print(f"{'='*65}")

    print("  Loading expired option contracts...")
    contracts = get_option_contracts(underlying, expiry, token)
    if not contracts:
        print(f"  No contracts found for {underlying} {expiry}")
        print("  Check: run --expiries to see available dates")
        return 0

    # Filter by strike range
    if min_strike is not None:
        contracts = [c for c in contracts if float(c["strike_price"]) >= min_strike]
    if max_strike is not None:
        contracts = [c for c in contracts if float(c["strike_price"]) <= max_strike]

    ce = [c for c in contracts if c.get("instrument_type") == "CE"]
    pe = [c for c in contracts if c.get("instrument_type") == "PE"]
    if not contracts:
        print(f"  No contracts in strike range {min_strike}-{max_strike}")
        return 0
    strikes = sorted({c["strike_price"] for c in ce})
    print(f"  CE: {len(ce)} | PE: {len(pe)} | strikes: {strikes[0]:.0f}-{strikes[-1]:.0f}")
    print(f"  Output: {exp_dir.resolve()}")
    print(f"\n  Fetching {len(contracts)} instruments...\n")

    suffix = interval.replace("minute", "min")
    total = skipped = updated = 0

    for inst in sorted(contracts, key=lambda x: (x["strike_price"], x["instrument_type"])):
        sym      = inst["trading_symbol"]
        ikey     = inst["instrument_key"]   # format: NSE_FO|73507|26-05-2026
        strike   = float(inst["strike_price"])
        opt_type = inst["instrument_type"]
        csv_path = exp_dir / f"{sym}_{suffix}.csv"

        if csv_path.exists():
            existing_rows = sum(1 for _ in csv_path.open()) - 1
            if existing_rows > 50:
                candles = fetch_candles(ikey, interval, expiry, expiry, token)
                n = append_missing(csv_path, candles, expiry, strike, opt_type)
                if n:
                    total += n
                    updated += 1
                    print(f"  {sym}: +{n} bars (update)")
                time.sleep(FETCH_DELAY)
                continue

        # Full contract lifetime: from listing date to expiry
        # Upstox returns full history in one call for expired contracts
        from_date = date(expiry.year - 1, expiry.month, 1)
        candles   = fetch_candles(ikey, interval, from_date, expiry, token)
        time.sleep(FETCH_DELAY)

        if not candles:
            skipped += 1
            print(f"  {sym}: 0 bars - skipped")
            continue

        write_csv(csv_path, candles, expiry, strike, opt_type)
        total += len(candles)
        print(f"  {sym}: {len(candles)} bars -> {csv_path.name}")

    print(f"\n  Total bars : {total:,}")
    print(f"  Updated    : {updated}")
    print(f"  Skipped    : {skipped}")
    return total


def _is_monthly_expiry(expiry: date, underlying: str) -> bool:
    """True if expiry is the last occurrence of its weekday in its month."""
    target_dow = MONTHLY_EXPIRY_DOW.get(underlying.upper(), 3)
    if expiry.weekday() != target_dow:
        return False
    return (expiry + timedelta(weeks=1)).month != expiry.month


def _expiry_already_done(underlying: str, expiry: date, interval: str) -> bool:
    """Return True if this expiry has at least 20 CSV files already written."""
    out_dir = Path(OUT_DIRS.get(underlying, f"data/{underlying.lower()}_options"))
    exp_dir = out_dir / f"{expiry.year}-{expiry.month:02d}"
    if not exp_dir.exists():
        return False
    suffix = interval.replace("minute", "min")
    csvs = list(exp_dir.glob(f"*{expiry.strftime('%d %b %y').upper()}*_{suffix}.csv"))
    return len(csvs) >= 100


def fetch_all_expiries(underlying: str, interval: str = "5minute",
                       monthly_only: bool = False,
                       min_strike: int | None = None,
                       max_strike: int | None = None,
                       from_date: date | None = None,
                       to_date: date | None = None) -> None:
    token = _load_token()
    und = underlying.upper()
    print(f"\nFetching {'monthly ' if monthly_only else ''}expired expiries for {und}...")
    expiries = get_expiries(und, token)
    print(f"Found {len(expiries)} total expiries: {expiries[0]} to {expiries[-1]}")

    if monthly_only:
        # Keep last expiry per calendar month (handles changing expiry weekdays)
        by_month: dict[str, str] = {}
        for e in expiries:
            by_month[e[:7]] = e  # YYYY-MM -> latest date in that month
        expiries = sorted(by_month.values())
        print(f"Monthly only (last per month): {len(expiries)} expiries")

    if from_date:
        expiries = [e for e in expiries if date.fromisoformat(e) >= from_date]
        print(f"From {from_date}: {len(expiries)} expiries")

    if to_date:
        expiries = [e for e in expiries if date.fromisoformat(e) <= to_date]
        print(f"To {to_date}: {len(expiries)} expiries")

    grand_total = 0
    skipped_expiries = 0
    for exp_str in expiries:
        exp = date.fromisoformat(exp_str)
        if _expiry_already_done(und, exp, interval):
            print(f"  {exp} - already downloaded, skipping")
            skipped_expiries += 1
            continue
        grand_total += fetch_expiry_options(und, exp, interval, min_strike, max_strike)
    print(f"\nGrand total: {grand_total:,} bars across {len(expiries)-skipped_expiries} expiries "
          f"({skipped_expiries} skipped - already done)")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Upstox historical expired options data (Plus plan required)")
    parser.add_argument("--login", action="store_true",
                        help="OAuth login - save access token")
    parser.add_argument("--expiries", metavar="UNDERLYING",
                        help="List available expired expiries (e.g. NIFTY)")
    parser.add_argument("underlying", nargs="?",
                        help="NIFTY | BANKNIFTY | MIDCPNIFTY | FINNIFTY")
    parser.add_argument("expiry", nargs="?",
                        help="Expiry date YYYY-MM-DD (omit with --all-expiries)")
    parser.add_argument("--all-expiries", action="store_true",
                        help="Fetch all available expired expiries for this underlying")
    parser.add_argument("--monthly-only", action="store_true",
                        help="Only fetch monthly expiries (skip weeklies)")
    parser.add_argument("--interval", default="5minute",
                        choices=["1minute", "3minute", "5minute", "15minute", "30minute", "day"],
                        help="Candle interval (default: 5minute)")
    parser.add_argument("--min-strike", type=int, default=None,
                        help="Minimum strike price to fetch (default: per-underlying preset)")
    parser.add_argument("--max-strike", type=int, default=None,
                        help="Maximum strike price to fetch (default: per-underlying preset)")
    parser.add_argument("--all-strikes", action="store_true",
                        help="Disable strike range filter and fetch all strikes")
    parser.add_argument("--from-date", default=None,
                        help="Only fetch expiries on or after this date (YYYY-MM-DD)")
    parser.add_argument("--to-date", default=None,
                        help="Only fetch expiries on or before this date (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.login:
        do_login()
        return

    if args.expiries:
        token    = _load_token()
        expiries = get_expiries(args.expiries.upper(), token)
        print(f"\n{args.expiries.upper()} available expired expiries ({len(expiries)}):")
        for e in expiries:
            print(f"  {e}")
        return

    if not args.underlying:
        parser.print_help()
        sys.exit(1)

    min_strike = None if args.all_strikes else args.min_strike
    max_strike = None if args.all_strikes else args.max_strike

    from_date = date.fromisoformat(args.from_date) if args.from_date else None
    to_date   = date.fromisoformat(args.to_date)   if args.to_date   else None

    if args.all_expiries:
        fetch_all_expiries(args.underlying.upper(), args.interval,
                           monthly_only=args.monthly_only,
                           min_strike=min_strike, max_strike=max_strike,
                           from_date=from_date, to_date=to_date)
        return

    if not args.expiry:
        print("Provide an expiry date (YYYY-MM-DD) or use --all-expiries")
        sys.exit(1)

    try:
        expiry = date.fromisoformat(args.expiry)
    except ValueError:
        print(f"Invalid expiry '{args.expiry}'. Use YYYY-MM-DD.")
        sys.exit(1)

    fetch_expiry_options(args.underlying.upper(), expiry, args.interval,
                         min_strike=min_strike, max_strike=max_strike)


if __name__ == "__main__":
    main()
