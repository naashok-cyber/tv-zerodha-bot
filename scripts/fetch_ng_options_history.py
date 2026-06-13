#!/usr/bin/env python3
"""
scripts/fetch_ng_options_history.py

Fetch 1 year of 5-minute OHLC data for MCX NATURALGAS options + futures
from Kite Connect, for IV seasonality analysis.

Usage (from project root, with .env + valid Kite session):
    python scripts/fetch_ng_options_history.py
    python scripts/fetch_ng_options_history.py --dry-run          # discovery only
    python scripts/fetch_ng_options_history.py --from 2025-06-01  # custom start
    python scripts/fetch_ng_options_history.py --force            # re-fetch existing CSVs
    python scripts/fetch_ng_options_history.py --underlying NATGASMINI

Output layout:
    data/ng_options/
        index.json                          ← master fetch manifest
        NG_futures_5min.csv                 ← 1-year continuous futures (back-stitched)
        {YYYY-MM}/                          ← one dir per expiry month
            {TRADINGSYMBOL}_5min.csv        ← per-contract option OHLC

IV calculation note:
    Kite instruments CSV only lists ACTIVE contracts — expired option tokens
    are not available.  Use the futures continuous series as the underlying
    price history; combine with active-contract option prices for Black-76 IV.

Requires:
    .env   — KITE_API_KEY, SECRET_KEY (and other app config)
    data/access_token.enc — fresh session from /kite/login or PYOTP_AUTO_LOGIN
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

# ── Bootstrap: make `app.*` importable from project root ─────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.kite_session import TokenStaleError, get_session_manager

# ── Config ────────────────────────────────────────────────────────────────────

NG_STRIKE_INTERVAL = 5.0       # Rs; confirmed from MCX instruments CSV (not 2.5)
ATM_HALF_WINDOW = 5            # fetch ATM ± this many strikes (11 strikes total)
API_DELAY_S = 0.35             # seconds between Kite API calls  (≈ 3 req/s limit)
MAX_DAYS_PER_CHUNK = 58        # Kite caps 5-min range at 60 days; stay 2 below
MCX_INSTRUMENTS_URL = "https://api.kite.trade/instruments/MCX"
DATA_ROOT = Path("data/ng_options")

CSV_OPTION_FIELDS = [
    "date", "time", "open", "high", "low", "close", "volume", "oi",
    "expiry", "strike", "option_type",
]
CSV_FUTURES_FIELDS = [
    "date", "time", "open", "high", "low", "close", "volume",
    "expiry", "tradingsymbol",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ng_fetch")


# ─────────────────────────────────────────────────────────────────────────────
# Instruments helpers
# ─────────────────────────────────────────────────────────────────────────────

def _download_mcx_instruments() -> list[dict]:
    log.info("Downloading MCX instruments from Kite…")
    resp = requests.get(MCX_INSTRUMENTS_URL, timeout=30)
    resp.raise_for_status()
    rows: list[dict] = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        expiry_str = row.get("expiry", "").strip()
        strike_str = row.get("strike", "").strip()
        try:
            rows.append({
                "instrument_token": int(row["instrument_token"]),
                "tradingsymbol":    row["tradingsymbol"].strip(),
                "name":             row["name"].strip(),
                "expiry":           date.fromisoformat(expiry_str) if expiry_str else None,
                "strike":           float(strike_str) if strike_str else None,
                "tick_size":        float(row.get("tick_size", 0.05)),
                "lot_size":         int(row.get("lot_size", 1)),
                "instrument_type":  row["instrument_type"].strip(),
                "segment":          row.get("segment", "").strip(),
                "exchange":         row.get("exchange", "MCX").strip(),
            })
        except (ValueError, KeyError):
            continue
    log.info("  Parsed %d MCX instruments total", len(rows))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Date / strike math
# ─────────────────────────────────────────────────────────────────────────────

def nearest_strike(price: float, interval: float) -> float:
    return round(round(price / interval) * interval, 4)


def date_chunks(start: date, end: date, chunk_days: int = MAX_DAYS_PER_CHUNK) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Kite API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_datetime(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _to_date(d: Any) -> date:
    return d.date() if isinstance(d, datetime) else d


def fetch_ohlc(
    kite,
    token: int,
    from_date: date,
    to_date: date,
    interval: str,
    *,
    oi: bool = True,
) -> list[dict]:
    """Call kite.historical_data; return empty list on any error."""
    try:
        bars = kite.historical_data(
            token,
            _to_datetime(from_date),
            _to_datetime(to_date).replace(hour=23, minute=59, second=59),
            interval,
            continuous=False,
            oi=oi,
        )
        time.sleep(API_DELAY_S)
        return bars or []
    except Exception as exc:
        log.warning("  [API error] token=%d %s→%s %s: %s", token, from_date, to_date, interval, exc)
        time.sleep(API_DELAY_S)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

def csv_row_count(path: Path) -> int:
    """Return number of data rows in a CSV (0 if not found)."""
    if not path.exists():
        return 0
    with path.open(newline="") as f:
        return max(0, sum(1 for _ in f) - 1)  # subtract header


def _bars_to_option_rows(
    bars: list[dict], expiry: date, strike: float, option_type: str
) -> list[dict]:
    rows = []
    for bar in bars:
        dt: datetime = bar["date"]
        rows.append({
            "date":        dt.strftime("%Y-%m-%d"),
            "time":        dt.strftime("%H:%M:%S"),
            "open":        bar.get("open", ""),
            "high":        bar.get("high", ""),
            "low":         bar.get("low", ""),
            "close":       bar.get("close", ""),
            "volume":      bar.get("volume", ""),
            "oi":          bar.get("oi", ""),
            "expiry":      expiry.isoformat(),
            "strike":      strike,
            "option_type": option_type,
        })
    return rows


def _bars_to_futures_rows(bars: list[dict], expiry: date, tradingsymbol: str) -> list[dict]:
    rows = []
    for bar in bars:
        dt: datetime = bar["date"]
        rows.append({
            "date":          dt.strftime("%Y-%m-%d"),
            "time":          dt.strftime("%H:%M:%S"),
            "open":          bar.get("open", ""),
            "high":          bar.get("high", ""),
            "low":           bar.get("low", ""),
            "close":         bar.get("close", ""),
            "volume":        bar.get("volume", ""),
            "expiry":        expiry.isoformat(),
            "tradingsymbol": tradingsymbol,
        })
    return rows


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch MCX NATURALGAS options 5-min history from Kite Connect."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Instrument discovery only; no historical data fetches.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if CSV already exists.")
    parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", default=None,
                        help="Override fetch start date (default: 1 year ago).")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", default=None,
                        help="Override fetch end date (default: today).")
    parser.add_argument("--underlying", default="NATURALGAS",
                        choices=["NATURALGAS", "NATGASMINI"],
                        help="Which NG underlying to fetch options for.")
    parser.add_argument("--strikes", type=int, default=ATM_HALF_WINDOW, metavar="N",
                        help=f"Fetch ATM ± N strikes (default: {ATM_HALF_WINDOW}).")
    args = parser.parse_args()

    today = date.today()
    end_date   = date.fromisoformat(args.to_date)   if args.to_date   else today
    start_date = date.fromisoformat(args.from_date) if args.from_date else (today - timedelta(days=365))
    underlying = args.underlying
    half_win   = args.strikes

    log.info("=" * 60)
    log.info("MCX %s options history fetch", underlying)
    log.info("Range: %s → %s   strikes: ATM ± %d", start_date, end_date, half_win)
    log.info("dry-run=%s   force=%s", args.dry_run, args.force)
    log.info("=" * 60)

    # ── 1 + 2. Instrument discovery (no auth needed — public Kite CSV) ────────
    all_instruments = _download_mcx_instruments()

    ng_options = [
        r for r in all_instruments
        if r["name"] == underlying
        and r["instrument_type"] in ("CE", "PE")
        and r["expiry"] is not None
    ]
    ng_futures = sorted(
        [r for r in all_instruments
         if r["name"] == underlying and r["instrument_type"] == "FUT" and r["expiry"]],
        key=lambda x: x["expiry"],
    )

    log.info("\nInstrument discovery:")
    log.info("  %s options: %d contracts across %d expiries",
             underlying, len(ng_options),
             len({r["expiry"] for r in ng_options}))
    log.info("  %s futures: %d contracts", underlying, len(ng_futures))

    if not ng_options:
        log.error("No %s options found in MCX instruments CSV.", underlying)
        log.error("Check that MCX lists %s with instrument_type CE/PE.", underlying)
        sys.exit(1)

    # Confirm strike interval from the data
    sample_strikes = sorted({r["strike"] for r in ng_options if r["strike"]})[: 10]
    if len(sample_strikes) >= 2:
        diffs = [sample_strikes[i+1] - sample_strikes[i] for i in range(len(sample_strikes)-1)]
        observed_interval = min(diffs)
        log.info("  Observed strike interval: %.2f Rs  (config: %.2f Rs)",
                 observed_interval, NG_STRIKE_INTERVAL)

    # Group by expiry; filter to relevant window
    by_expiry: dict[date, list[dict]] = defaultdict(list)
    for r in ng_options:
        by_expiry[r["expiry"]].append(r)

    # Include all currently listed expiries: Kite only shows active contracts so
    # every expiry here has live data.  We fetch backwards as far as Kite allows
    # (typically ~3 months for 5-min options data on the front-month contract).
    relevant_expiries = sorted(by_expiry.keys())

    log.info("\nRelevant expiries (%d):", len(relevant_expiries))
    for exp in relevant_expiries:
        n_ce = sum(1 for r in by_expiry[exp] if r["instrument_type"] == "CE")
        n_pe = sum(1 for r in by_expiry[exp] if r["instrument_type"] == "PE")
        log.info("  %s — CE: %d  PE: %d  strikes: %s",
                 exp, n_ce, n_pe,
                 sorted({r["strike"] for r in by_expiry[exp]})[:5])

    if not relevant_expiries:
        log.warning("No relevant expiries in range.  Available: %s",
                    sorted(by_expiry.keys()))
        sys.exit(1)

    if args.dry_run:
        log.info("\n[dry-run] Stopping here — no historical data fetched.")
        sys.exit(0)

    # ── 3. Kite session (only needed for historical data fetches) ─────────────
    try:
        kite = get_session_manager().get_kite()
        log.info("Kite session OK")
    except TokenStaleError as exc:
        log.error("Kite session stale: %s", exc)
        log.error("Complete daily login at /kite/login (or wait for PYOTP_AUTO_LOGIN).")
        sys.exit(1)

    # ── 4. Futures daily data → ATM mapping ───────────────────────────────────
    log.info("\n=== Phase 3: NG futures daily OHLC (for ATM) ===")

    futures_close: dict[date, float] = {}  # date → front-month close

    # ng_futures is sorted ascending by expiry, so oldest (nearest) expiry is iterated first.
    # First-write-wins gives front-month prices: for any date where two contracts overlap,
    # the one with the smaller (sooner) expiry wins, which is correct for front-month stitching.
    for fut in ng_futures:
        exp = fut["expiry"]
        # Fetch the period when this contract was the front-month (roughly its last 3 months)
        fetch_from = max(start_date, exp - timedelta(days=100))
        fetch_to   = min(end_date, exp)
        if fetch_from > fetch_to:
            continue

        log.info("  %s (token=%d) daily: %s → %s",
                 fut["tradingsymbol"], fut["instrument_token"], fetch_from, fetch_to)
        bars = fetch_ohlc(kite, fut["instrument_token"], fetch_from, fetch_to, "day", oi=False)
        for bar in bars:
            d = _to_date(bar["date"])
            if d not in futures_close:  # front-month wins; don't overwrite with back-month
                futures_close[d] = bar["close"]
        log.info("    → %d daily bars", len(bars))

    if futures_close:
        log.info("  ATM reference prices for %d dates (%s → %s)",
                 len(futures_close), min(futures_close), max(futures_close))
    else:
        log.warning("  No futures daily data returned — ATM strike selection will use strike-median fallback")

    # ── 5. Select strike universe per expiry ──────────────────────────────────
    log.info("\n=== Phase 4: Strike selection ===")

    selected: dict[date, list[float]] = {}  # expiry → sorted strike list

    for exp in relevant_expiries:
        contracts = by_expiry[exp]
        all_strikes = sorted({r["strike"] for r in contracts if r["strike"] is not None})
        if not all_strikes:
            log.warning("  %s: no strikes found — skipping", exp)
            continue

        # ATM = median futures price during this contract's delivery month
        month_prices = [
            v for d, v in futures_close.items()
            if (exp - timedelta(days=40)) <= d <= exp
        ]
        if month_prices:
            typical_fut = sorted(month_prices)[len(month_prices) // 2]
            atm = nearest_strike(typical_fut, NG_STRIKE_INTERVAL)
        else:
            atm = all_strikes[len(all_strikes) // 2]

        # Find ATM index; clamp window to available strikes
        atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm))
        lo  = max(0, atm_idx - half_win)
        hi  = min(len(all_strikes) - 1, atm_idx + half_win)
        chosen = all_strikes[lo : hi + 1]
        selected[exp] = chosen

        log.info("  %s: typical_fut=%.2f  ATM=%.2f  strikes=%s",
                 exp, typical_fut if month_prices else float("nan"), atm, chosen)

    # ── 6. Fetch 5-min option OHLC ────────────────────────────────────────────
    log.info("\n=== Phase 5: 5-min option OHLC fetch ===")

    index: dict[str, Any] = {
        "fetch_date":          today.isoformat(),
        "underlying":          underlying,
        "date_range":          {"from": start_date.isoformat(), "to": end_date.isoformat()},
        "strike_interval":     NG_STRIKE_INTERVAL,
        "atm_window":          half_win,
        "expiries_processed":  [],
        "strikes_per_expiry":  {},
        "token_coverage":      {},
        "futures_coverage":    {},
        "total_option_rows":   0,
        "total_futures_rows":  0,
        "gaps":                [],
    }

    total_option_rows = 0

    for exp in sorted(relevant_expiries):  # oldest first
        if exp not in selected:
            continue
        chosen_strikes = selected[exp]

        month_str = exp.strftime("%Y-%m")
        exp_dir = DATA_ROOT / month_str
        exp_dir.mkdir(parents=True, exist_ok=True)

        exp_fetch_from = max(start_date, exp - timedelta(days=100))
        exp_fetch_to   = min(end_date, exp)

        log.info("\n  Expiry %s (%d strikes)  range: %s → %s",
                 exp, len(chosen_strikes), exp_fetch_from, exp_fetch_to)

        index["expiries_processed"].append(exp.isoformat())
        index["strikes_per_expiry"][exp.isoformat()] = [
            {"strike": s} for s in chosen_strikes
        ]
        exp_rows = 0

        for strike in chosen_strikes:
            for opt_type in ("CE", "PE"):
                matching = [
                    r for r in by_expiry[exp]
                    if r["strike"] == strike and r["instrument_type"] == opt_type
                ]
                if not matching:
                    log.debug("    No token: %s %s %.2f", exp, opt_type, strike)
                    index["gaps"].append({
                        "expiry": exp.isoformat(), "strike": strike,
                        "type": opt_type, "reason": "token not in instruments CSV",
                    })
                    continue

                rec    = matching[0]
                token  = rec["instrument_token"]
                symbol = rec["tradingsymbol"]
                csv_path = exp_dir / f"{symbol}_5min.csv"

                # Resumability
                if not args.force and csv_row_count(csv_path) > 0:
                    n = csv_row_count(csv_path)
                    log.info("    SKIP  %-40s  (%d rows already)", symbol, n)
                    total_option_rows += n
                    exp_rows += n
                    index["token_coverage"][symbol] = {
                        "token": token, "rows": n, "status": "skipped_existing",
                    }
                    continue

                log.info("    FETCH %-40s  token=%-10d  %s→%s",
                         symbol, token, exp_fetch_from, exp_fetch_to)

                # Quick daily probe to check how far back Kite has data
                daily = fetch_ohlc(kite, token, exp_fetch_from, exp_fetch_to, "day", oi=True)
                if not daily:
                    log.warning("      No daily data — marking as gap")
                    index["gaps"].append({
                        "expiry": exp.isoformat(), "strike": strike,
                        "type": opt_type, "symbol": symbol, "token": token,
                        "reason": "no daily data from Kite",
                    })
                    index["token_coverage"][symbol] = {
                        "token": token, "rows": 0, "status": "no_data",
                    }
                    continue

                actual_from = _to_date(daily[0]["date"])
                actual_to   = _to_date(daily[-1]["date"])
                log.info("      Daily coverage: %s → %s (%d days)", actual_from, actual_to, len(daily))

                # Fetch 5-min in ≤60-day chunks
                all_rows: list[dict] = []
                for chunk_from, chunk_to in date_chunks(actual_from, actual_to):
                    bars = fetch_ohlc(kite, token, chunk_from, chunk_to, "5minute", oi=True)
                    rows = _bars_to_option_rows(bars, exp, strike, opt_type)
                    all_rows.extend(rows)
                    log.info("      chunk %s→%s: %d bars", chunk_from, chunk_to, len(bars))

                if all_rows:
                    write_csv(csv_path, all_rows, CSV_OPTION_FIELDS)
                    log.info("      → Wrote %d rows to %s", len(all_rows), csv_path.relative_to(DATA_ROOT))
                    index["token_coverage"][symbol] = {
                        "token":  token,
                        "rows":   len(all_rows),
                        "from":   actual_from.isoformat(),
                        "to":     actual_to.isoformat(),
                        "status": "ok",
                    }
                else:
                    log.warning("      No 5-min bars returned for %s", symbol)
                    index["gaps"].append({
                        "expiry": exp.isoformat(), "strike": strike,
                        "type": opt_type, "symbol": symbol, "token": token,
                        "reason": "no 5-min bars despite daily data",
                    })
                    index["token_coverage"][symbol] = {
                        "token": token, "rows": 0, "status": "no_5min",
                    }

                n = len(all_rows)
                total_option_rows += n
                exp_rows += n

        log.info("  Expiry %s: %d option rows", exp, exp_rows)

    # ── 7. Futures 5-min data ─────────────────────────────────────────────────
    log.info("\n=== Phase 6: NG futures 5-min OHLC ===")

    futures_csv = DATA_ROOT / "NG_futures_5min.csv"

    if not args.force and csv_row_count(futures_csv) > 0:
        n = csv_row_count(futures_csv)
        log.info("Futures CSV exists (%d rows) — skipping (use --force to re-fetch)", n)
        index["total_futures_rows"] = n
    else:
        all_fut_rows: list[dict] = []

        if not ng_futures:
            log.warning("No %s futures found in instruments", underlying)
        else:
            # MCX futures do not support continuous=True for sub-day intervals.
            # Fetch each listed contract individually; each gives ~3 months back.
            for fut in ng_futures:
                exp = fut["expiry"]
                # Fetch as far back as Kite allows (up to start_date)
                fetch_from = max(start_date, exp - timedelta(days=100))
                fetch_to   = min(end_date, exp)
                if fetch_from > fetch_to:
                    continue

                log.info("  %s (token=%d)  %s → %s",
                         fut["tradingsymbol"], fut["instrument_token"], fetch_from, fetch_to)

                contract_rows: list[dict] = []
                for chunk_from, chunk_to in date_chunks(fetch_from, fetch_to):
                    bars = fetch_ohlc(kite, fut["instrument_token"], chunk_from, chunk_to,
                                      "5minute", oi=False)
                    rows = _bars_to_futures_rows(bars, exp, fut["tradingsymbol"])
                    contract_rows.extend(rows)
                    log.info("    chunk %s→%s: %d bars", chunk_from, chunk_to, len(bars))

                all_fut_rows.extend(contract_rows)
                if contract_rows:
                    index["futures_coverage"][fut["tradingsymbol"]] = {
                        "token":  fut["instrument_token"],
                        "expiry": exp.isoformat(),
                        "rows":   len(contract_rows),
                    }
                log.info("  → %d rows for %s", len(contract_rows), fut["tradingsymbol"])

            if all_fut_rows:
                write_csv(futures_csv, all_fut_rows, CSV_FUTURES_FIELDS)
                log.info("Wrote %d futures rows → %s", len(all_fut_rows), futures_csv)
            else:
                log.warning("No futures 5-min data returned for any listed contract")

        index["total_futures_rows"] = len(all_fut_rows)

    # ── 8. Write index.json ───────────────────────────────────────────────────
    index["total_option_rows"] = total_option_rows
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    index_path = DATA_ROOT / "index.json"
    with index_path.open("w") as f:
        json.dump(index, f, indent=2)
    log.info("\nWrote manifest → %s", index_path)

    # ── 9. Coverage report ────────────────────────────────────────────────────
    log.info("\n%s", "=" * 60)
    log.info("COVERAGE REPORT")
    log.info("=" * 60)
    log.info("Fetch range:         %s → %s", start_date, end_date)
    log.info("Expiries processed:  %d", len(index["expiries_processed"]))
    log.info("Total option rows:   %d", total_option_rows)
    log.info("Total futures rows:  %d", index["total_futures_rows"])
    log.info("Gaps / missing:      %d", len(index["gaps"]))

    log.info("\nPer-expiry summary:")
    for exp_iso in index["expiries_processed"]:
        strikes = index["strikes_per_expiry"].get(exp_iso, [])
        tokens_ok = [
            sym for sym, cov in index["token_coverage"].items()
            if exp_iso.replace("-", "")[:6] in sym  # rough match on YYYYMM
            and cov.get("status") == "ok"
        ]
        tokens_gap = [
            g for g in index["gaps"]
            if g.get("expiry") == exp_iso
        ]
        log.info("  %s  strikes=%d  fetched_ok=%d  gaps=%d",
                 exp_iso, len(strikes), len(tokens_ok), len(tokens_gap))

    if index["gaps"]:
        log.info("\nFirst 15 gaps:")
        for g in index["gaps"][:15]:
            log.info("  %s", g)
        if len(index["gaps"]) > 15:
            log.info("  … %d more — see %s", len(index["gaps"]) - 15, index_path)

    log.info("\nData stored under: %s", DATA_ROOT.resolve())
    log.info("Done.")


if __name__ == "__main__":
    main()
