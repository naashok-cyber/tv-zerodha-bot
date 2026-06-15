"""Expiry-day options snapshot fetcher.

Fetches complete 5-min history for all strikes of the current expiry on expiry day.
The key insight: tokens are still active on expiry day so Kite returns the full
contract lifetime in one call. Within ~24-48 hours after expiry the tokens go dark.

Two scheduler jobs, two market sessions:

  NSE FNO (NIFTY, BANKNIFTY, MIDCPNIFTY):
    run_nse_expiry_snapshot_job() — 15:31 IST, right after NSE close.
    Weekly expiries: NIFTY=Thu, BANKNIFTY=Wed, MIDCPNIFTY=Mon.

  MCX (NATURALGAS):
    run_mcx_expiry_snapshot_job() — 22:25 IST, 1 hour before EOD.
    MCX data lags on Kite right after close, so pre-EOD fetch is more reliable.

Saves to data/{underlying}_options/{YYYY-MM}/{SYMBOL}_5min.csv
"""
from __future__ import annotations

import csv
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import IST

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

UNDERLYINGS: dict[str, dict] = {
    # NSE FNO — fetched at 15:31 IST right after market close
    "NIFTY": {
        "exchange": "NFO",
        "out_dir": "data/nifty_options",
        "eod_time": (15, 30),
    },
    "BANKNIFTY": {
        "exchange": "NFO",
        "out_dir": "data/banknifty_options",
        "eod_time": (15, 30),
    },
    "MIDCPNIFTY": {
        "exchange": "NFO",
        "out_dir": "data/midcpnifty_options",
        "eod_time": (15, 30),
    },
    # MCX — fetched at 22:25 IST (1 hr before EOD) to avoid post-close data lag
    "NATURALGAS": {
        "exchange": "MCX",
        "out_dir": "data/ng_options",
        "eod_time": (23, 30),
    },
}

NSE_FNO_UNDERLYINGS: set[str] = {"NIFTY", "BANKNIFTY", "MIDCPNIFTY"}
MCX_UNDERLYINGS: set[str]     = {"NATURALGAS"}

FETCH_DELAY_SEC = 0.38   # stay under Kite's 3 req/sec historical limit
CHUNK_DAYS      = 60     # Kite caps per-request history at ~60 days for 5min


# ── Core helpers ───────────────────────────────────────────────────────────────

def fetch_chunked(kite, token: int, start: datetime, end: datetime) -> list[dict]:
    """Fetch 5-min bars in 60-day chunks to stay within Kite's window limit."""
    all_bars: list[dict] = []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), end)
        try:
            bars = kite.historical_data(
                token, cur, chunk_end, "5minute", continuous=False, oi=True
            ) or []
            all_bars.extend(bars)
        except Exception as exc:
            log.warning("chunk %s–%s token=%d ERR: %s", cur.date(), chunk_end.date(), token, exc)
        time.sleep(FETCH_DELAY_SEC)
        cur = chunk_end + timedelta(seconds=1)
    return all_bars


def find_current_expiry(instruments: list[dict], underlying: str) -> date | None:
    """Return the nearest upcoming (or today's) expiry for the underlying."""
    today = date.today()
    opts = [
        r for r in instruments
        if r.get("name") == underlying
        and r.get("instrument_type") in ("CE", "PE", "OPT")
        and r.get("expiry") is not None
        and r["expiry"] >= today
    ]
    if not opts:
        return None
    return min(opts, key=lambda r: r["expiry"])["expiry"]


def is_expiry_today(instruments: list[dict], underlying: str) -> bool:
    exp = find_current_expiry(instruments, underlying)
    return exp == date.today() if exp else False


def _opt_type(o: dict) -> str:
    it = (o.get("instrument_type") or "").upper()
    sym = (o.get("tradingsymbol") or "").upper()
    if it in ("CE", "PE"):
        return it
    if sym.endswith("CE"):
        return "CE"
    if sym.endswith("PE"):
        return "PE"
    return it


def _write_csv(path: Path, bars: list[dict], expiry: date, strike: float, opt_type: str) -> None:
    fields = ["date", "time", "open", "high", "low", "close", "volume", "oi",
              "expiry", "strike", "option_type"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for b in bars:
            dt: datetime = b["date"]
            w.writerow({
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M:%S"),
                "open": b["open"], "high": b["high"],
                "low": b["low"],   "close": b["close"],
                "volume": b.get("volume", ""),
                "oi":     b.get("oi", ""),
                "expiry": str(expiry),
                "strike": strike,
                "option_type": opt_type,
            })


def _append_missing(csv_path: Path, new_bars: list[dict], fields: list[str],
                    expiry: date, strike: float, opt_type: str) -> None:
    existing_keys: set[str] = set()
    with csv_path.open("r") as f:
        for row in csv.DictReader(f):
            existing_keys.add(f"{row['date']} {row['time']}")

    new_rows = []
    for b in new_bars:
        dt: datetime = b["date"]
        key = f"{dt.strftime('%Y-%m-%d')} {dt.strftime('%H:%M:%S')}"
        if key not in existing_keys:
            new_rows.append({
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M:%S"),
                "open": b["open"], "high": b["high"],
                "low": b["low"],   "close": b["close"],
                "volume": b.get("volume", ""),
                "oi":     b.get("oi", ""),
                "expiry": str(expiry),
                "strike": strike,
                "option_type": opt_type,
            })

    if new_rows:
        with csv_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writerows(new_rows)


# ── Main fetch logic ───────────────────────────────────────────────────────────

def fetch_snapshot(kite, underlying: str, cfg: dict, force: bool = False) -> int:
    """Fetch all strikes for the nearest expiry. Returns total bars saved."""
    exchange = cfg["exchange"]
    out_root = Path(cfg["out_dir"])
    eod_h, eod_m = cfg["eod_time"]

    log.info("[expiry_snapshot] %s — loading %s instruments", underlying, exchange)
    instruments = kite.instruments(exchange)
    time.sleep(0.5)

    expiry = find_current_expiry(instruments, underlying)
    if expiry is None:
        log.warning("[expiry_snapshot] %s: no upcoming expiry found — skipping", underlying)
        return 0

    log.info("[expiry_snapshot] %s: current expiry=%s", underlying, expiry)

    if not force and expiry != date.today():
        log.info(
            "[expiry_snapshot] %s: today is not expiry day (%s) — skipping (use force=True to override)",
            underlying, expiry,
        )
        return 0

    # Collect all option instruments for this expiry
    opts = [
        r for r in instruments
        if r.get("name") == underlying
        and r.get("expiry") == expiry
        and r.get("instrument_type") in ("CE", "PE", "OPT")
    ]
    # Also catch MCX-OPT segment instruments not tagged CE/PE
    opts += [
        r for r in instruments
        if r.get("name") == underlying
        and r.get("expiry") == expiry
        and r.get("segment") == "MCX-OPT"
        and r not in opts
    ]
    # Deduplicate by token
    seen: set[int] = set()
    deduped: list[dict] = []
    for o in opts:
        if o["instrument_token"] not in seen:
            seen.add(o["instrument_token"])
            deduped.append(o)
    opts = deduped

    strikes_ce = sorted({float(o["strike"]) for o in opts if _opt_type(o) == "CE"})
    strikes_pe = sorted({float(o["strike"]) for o in opts if _opt_type(o) == "PE"})
    log.info(
        "[expiry_snapshot] %s: %d CE strikes, %d PE strikes, range %s–%s",
        underlying, len(strikes_ce), len(strikes_pe),
        f"{strikes_ce[0]:.0f}" if strikes_ce else "—",
        f"{strikes_ce[-1]:.0f}" if strikes_ce else "—",
    )

    exp_dir = out_root / f"{expiry.year}-{expiry.month:02d}"
    exp_dir.mkdir(parents=True, exist_ok=True)

    now_ist = datetime.now(IST).replace(tzinfo=None)
    fetch_end = min(
        now_ist,
        datetime(expiry.year, expiry.month, expiry.day, eod_h, eod_m),
    )
    fetch_start = datetime(expiry.year - 1, expiry.month, 1)

    log.info(
        "[expiry_snapshot] %s: fetch window %s → %s, %d instruments",
        underlying, fetch_start.date(), fetch_end.strftime("%Y-%m-%d %H:%M"), len(opts),
    )

    fields = ["date", "time", "open", "high", "low", "close", "volume", "oi",
              "expiry", "strike", "option_type"]

    total_bars = skipped = updated = 0

    for o in sorted(opts, key=lambda x: (float(x.get("strike", 0)), x.get("tradingsymbol", ""))):
        sym      = o["tradingsymbol"]
        token    = o["instrument_token"]
        strike   = float(o.get("strike", 0))
        opt_type = _opt_type(o)
        csv_path = exp_dir / f"{sym}_5min.csv"

        if csv_path.exists():
            existing_rows = sum(1 for _ in csv_path.open()) - 1
            if existing_rows > 100:
                partial_start = fetch_end - timedelta(days=2)
                bars = fetch_chunked(kite, token, partial_start, fetch_end)
                if bars:
                    _append_missing(csv_path, bars, fields, expiry, strike, opt_type)
                    total_bars += len(bars)
                    updated += 1
                    log.debug("[expiry_snapshot] %s %s: +%d bars (update)", underlying, sym, len(bars))
                continue

        bars = fetch_chunked(kite, token, fetch_start, fetch_end)
        if not bars:
            skipped += 1
            log.debug("[expiry_snapshot] %s %s: 0 bars — skipped", underlying, sym)
            continue

        _write_csv(csv_path, bars, expiry, strike, opt_type)
        total_bars += len(bars)
        log.debug("[expiry_snapshot] %s %s: %d bars saved", underlying, sym, len(bars))

    log.info(
        "[expiry_snapshot] %s done: %d bars, %d updated, %d skipped → %s",
        underlying, total_bars, updated, skipped, exp_dir,
    )
    return total_bars


# ── Scheduler jobs ─────────────────────────────────────────────────────────────

def run_nse_expiry_snapshot_job(session_factory=None) -> None:
    """APScheduler job at 15:31 IST — NSE FNO expiry day data collection."""
    import app.state as state
    from app.kite_session import get_session_manager

    if state.get_session_invalid():
        log.warning("[expiry_snapshot_nse] session invalid — skipping")
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[expiry_snapshot_nse] cannot get kite: %s", exc)
        return

    today = date.today()
    log.info("[expiry_snapshot_nse] checking NFO expiry for %s...", today)

    try:
        instruments = kite.instruments("NFO")
        time.sleep(0.5)
    except Exception as exc:
        log.error("[expiry_snapshot_nse] instruments(NFO) failed: %s", exc)
        return

    any_fetched = False
    for underlying in sorted(NSE_FNO_UNDERLYINGS):
        if is_expiry_today(instruments, underlying):
            log.info("[expiry_snapshot_nse] %s expires today — fetching", underlying)
            try:
                n = fetch_snapshot(kite, underlying, UNDERLYINGS[underlying], force=True)
                log.info("[expiry_snapshot_nse] %s: %d bars saved", underlying, n)
                any_fetched = True
            except Exception as exc:
                log.error("[expiry_snapshot_nse] %s fetch failed: %s", underlying, exc, exc_info=True)

    if not any_fetched:
        log.info("[expiry_snapshot_nse] no NFO expiries today (%s)", today)


def run_mcx_expiry_snapshot_job(session_factory=None) -> None:
    """APScheduler job at 22:25 IST — MCX expiry day data collection."""
    import app.state as state
    from app.kite_session import get_session_manager

    if state.get_session_invalid():
        log.warning("[expiry_snapshot_mcx] session invalid — skipping")
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.error("[expiry_snapshot_mcx] cannot get kite: %s", exc)
        return

    today = date.today()
    log.info("[expiry_snapshot_mcx] checking MCX expiry for %s...", today)

    try:
        instruments = kite.instruments("MCX")
        time.sleep(0.5)
    except Exception as exc:
        log.error("[expiry_snapshot_mcx] instruments(MCX) failed: %s", exc)
        return

    any_fetched = False
    for underlying in sorted(MCX_UNDERLYINGS):
        if is_expiry_today(instruments, underlying):
            log.info("[expiry_snapshot_mcx] %s expires today — fetching", underlying)
            try:
                n = fetch_snapshot(kite, underlying, UNDERLYINGS[underlying], force=True)
                log.info("[expiry_snapshot_mcx] %s: %d bars saved", underlying, n)
                any_fetched = True
            except Exception as exc:
                log.error("[expiry_snapshot_mcx] %s fetch failed: %s", underlying, exc, exc_info=True)

    if not any_fetched:
        log.info("[expiry_snapshot_mcx] no MCX expiries today (%s)", today)
