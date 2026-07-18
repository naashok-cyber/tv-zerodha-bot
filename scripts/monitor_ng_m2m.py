"""
NG M2M ladder exit with adaptive monitoring loop.

Ladder:
  - Tier 1 at HALF_EXIT_THRESHOLD : exit ceil(qty/2) of each open leg
  - Tier N every INCREMENT_INR after that: exit 1 lot from each leg

Usage:
  # single shot (manual / cron)
  python3 scripts/monitor_ng_m2m.py

  # adaptive self-looping daemon (recommended)
  python3 scripts/monitor_ng_m2m.py --loop

Loop cadence:
  NORMAL (2 min) → FAST (1 min) when:
    • M2M within 10% of next trigger  (proximity)
    • M2M drops >20% vs previous reading and |prev| > ₹2,000  (drop)
  FAST → NORMAL when:
    • proximity-triggered: M2M exits 90%-of-trigger zone AND stable 10×
    • drop-triggered     : M2M recovers 20%+ of trigger from trough  OR  stable 10×
  "stable" = |ΔM2M| <= 5% of trigger for N consecutive 1-min reads
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import sqlite3 as _sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scipy.stats import norm

from app.kite_session import TokenStaleError, get_session_manager

HALF_EXIT_THRESHOLD = 16_625.0
INCREMENT_INR       = 2_000.0
STATE_PATH          = "/tmp/ng_exit_state.json"
TICK                = 0.05
EXCHANGE            = "MCX"
PRODUCT             = "NRML"
LOT_SIZE            = 1250    # NATURALGAS contract multiplier
WIDE_SPREAD         = 0.50   # bid-ask spread above this → warn and skip exits
LIMIT_WAIT_SECONDS  = 15
OPTION_EXPIRY       = date(2026, 7, 28)   # approx MCX NG Jul expiry — update monthly
RISK_FREE_RATE      = 0.065
IST                 = timezone(timedelta(hours=5, minutes=30))

# ── ADX-based delta hedge bands ───────────────────────────────────────────────
ADX_PERIOD          = 14
ADX_CANDLE_INTERVAL = "10minute"
ADX_CHOPPY_MAX      = 20.0    # ADX < 20  → choppy/range-bound → wide band
ADX_TREND_MIN       = 25.0    # ADX > 25  → trending           → tight band
HEDGE_BAND_CHOPPY   = 700     # ₹ threshold when choppy
HEDGE_BAND_MILD     = 500     # ₹ threshold when mild trend (ADX 20–25)
HEDGE_BAND_TREND    = 350     # ₹ threshold when trending

# ── adaptive loop constants ───────────────────────────────────────────────────
NORMAL_INTERVAL  = 120    # 2-min sleep in normal mode
FAST_INTERVAL    = 60     # 1-min sleep in fast mode
PROXIMITY_PCT    = 0.10   # enter FAST when M2M >= (1 - 0.10) * trigger
DROP_PCT         = 0.20   # enter FAST when M2M drops > 20% vs prev
RECOVERY_PCT     = 0.20   # exit FAST (drop) when M2M recovers 20%+ of trigger from trough
STABILIZE_PCT    = 0.05   # |ΔM2M| / trigger <= 5% counts as one stable tick
STABILIZE_COUNT  = 10     # consecutive stable ticks needed to exit FAST
MIN_M2M_FOR_DROP    = 2_000  # drop-rule only fires when |prev_m2m| >= this
PRE_EVENING_START   = dtime(18, 30)   # force 1-min interval from 18:30 IST
PRE_EVENING_END     = dtime(20, 30)   # back to normal cadence after 20:30 IST


# ── time helpers ─────────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(IST)


def is_pre_evening() -> bool:
    """True 18:30–20:30 IST — inter-session gap before MCX evening open."""
    t = now_ist().time()
    return PRE_EVENING_START <= t <= PRE_EVENING_END


# ── helpers ──────────────────────────────────────────────────────────────────

def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


def is_ng(symbol: str) -> bool:
    return "NATURALGAS" in symbol or "NATGAS" in symbol


def _get_ng_futures_token() -> int | None:
    try:
        conn = _sqlite3.connect("/app/data/bot.db")
        cur = conn.cursor()
        cur.execute(
            "SELECT instrument_token FROM instruments "
            "WHERE name='NATURALGAS' AND instrument_type='FUT' AND exchange='MCX' "
            "ORDER BY expiry ASC LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def fetch_adx(kite) -> float | None:
    from app.adx import compute_adx
    token = _get_ng_futures_token()
    if token is None:
        return None
    n_candles = ADX_PERIOD * 3 + 5
    now = now_ist()
    from_dt = now - timedelta(minutes=n_candles * 10 + 60)
    try:
        raw = kite.historical_data(token, from_dt, now, ADX_CANDLE_INTERVAL)
    except Exception as exc:
        print(f"    [ADX-WARN] {exc}")
        return None
    return compute_adx(raw, period=ADX_PERIOD)


def adx_hedge_band(adx: float | None) -> tuple[int, str]:
    if adx is None:
        return HEDGE_BAND_MILD, "UNKNOWN"
    if adx < ADX_CHOPPY_MAX:
        return HEDGE_BAND_CHOPPY, "CHOPPY"
    if adx < ADX_TREND_MIN:
        return HEDGE_BAND_MILD, "MILD"
    return HEDGE_BAND_TREND, "TRENDING"


def fetch_ng_legs(kite):
    positions = kite.positions()
    net = positions.get("net", []) or []
    ng = [p for p in net if is_ng(p.get("tradingsymbol") or "")]
    return [p for p in ng if (p.get("quantity") or 0) != 0 or (p.get("pnl") or 0) != 0]


def open_legs(legs):
    return [p for p in legs if (p.get("quantity") or 0) != 0]


def get_mark_prices(kite, legs: list) -> dict:
    """Fetch bid/ask midpoint via kite.quote() for each leg.
    Falls back to quote last_price if one side is missing.
    Returns {} on any API error so caller can use positions LTP.
    Each value: {"price": float, "bid": float, "ask": float, "spread": float|None}"""
    symbols = [f"{EXCHANGE}:{p['tradingsymbol']}" for p in legs if p.get("tradingsymbol")]
    if not symbols:
        return {}
    try:
        quotes = kite.quote(symbols)
    except Exception as exc:
        print(f"    [QUOTE-WARN] {exc}")
        return {}
    result = {}
    for p in legs:
        sym = p.get("tradingsymbol", "")
        q = quotes.get(f"{EXCHANGE}:{sym}", {})
        if not q:
            continue
        depth = q.get("depth") or {}
        buys  = depth.get("buy")  or []
        sells = depth.get("sell") or []
        bid = float(buys[0]["price"])  if buys  and buys[0].get("price")  else 0.0
        ask = float(sells[0]["price"]) if sells and sells[0].get("price") else 0.0
        spread = round(ask - bid, 2) if bid > 0 and ask > 0 else None
        if bid > 0 and ask > 0:
            price = round_tick((bid + ask) / 2)
        elif q.get("last_price"):
            price = float(q["last_price"])
        else:
            continue
        result[sym] = {"price": price, "bid": bid, "ask": ask, "spread": spread}
    return result


def apply_mark_prices(legs: list, mark: dict) -> list:
    """Return new legs list with last_price + pnl recalculated from quote mark prices."""
    out = []
    for p in legs:
        p = dict(p)
        sym = p.get("tradingsymbol", "")
        if sym in mark:
            info = mark[sym]
            mp   = info["price"]
            avg  = float(p.get("average_price") or 0)
            qty  = int(p.get("quantity") or 0)
            p["last_price"] = mp
            p["pnl"]        = (mp - avg) * qty * LOT_SIZE
            p["_quote"]     = True
            p["_spread"]    = info["spread"]
        out.append(p)
    return out


# ── state ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"half_exit_done": False, "increments_done": 0}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def next_threshold(state: dict) -> float:
    if not state["half_exit_done"]:
        return HALF_EXIT_THRESHOLD
    return HALF_EXIT_THRESHOLD + INCREMENT_INR * (state["increments_done"] + 1)


# ── delta helpers ────────────────────────────────────────────────────────────

def _time_to_expiry() -> float:
    days = (OPTION_EXPIRY - date.today()).days
    return max(days, 1) / 365.0


def _solve_iv(price: float, F: float, K: float, T: float, flag: str) -> float:
    r = RISK_FREE_RATE
    lo, hi = 0.001, 10.0
    for _ in range(120):
        mid = (lo + hi) / 2
        d1 = (math.log(F / K) + 0.5 * mid**2 * T) / (mid * math.sqrt(T))
        d2 = d1 - mid * math.sqrt(T)
        if flag == "c":
            val = math.exp(-r * T) * (F * norm.cdf(d1) - K * norm.cdf(d2))
        else:
            val = math.exp(-r * T) * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        if val > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _unit_delta(ltp: float, F: float, K: float, T: float, flag: str) -> float | None:
    try:
        if ltp <= 0 or F <= 0 or K <= 0 or T <= 0:
            return None
        iv = _solve_iv(ltp, F, K, T, flag)
        r = RISK_FREE_RATE
        d1 = (math.log(F / K) + 0.5 * iv**2 * T) / (iv * math.sqrt(T))
        if flag == "c":
            return math.exp(-r * T) * norm.cdf(d1)
        else:
            return -math.exp(-r * T) * norm.cdf(-d1)
    except Exception:
        return None


# ── display ──────────────────────────────────────────────────────────────────

def print_state(legs, total_pnl, state, adx_info: dict | None = None) -> float:
    """Print position state. Returns net_delta_inr (net_delta * LOT_SIZE)."""
    ts = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
    ol = sum(1 for p in legs if (p.get("quantity") or 0) != 0)
    nxt = next_threshold(state)
    half = "DONE" if state["half_exit_done"] else "pending"
    print(
        f"[{ts}] NG_M2M=₹{total_pnl:.2f}  open_legs={ol}  "
        f"half={half}  increments={state['increments_done']}  next_trigger=₹{nxt:.0f}"
    )
    fut_ltp = next(
        (float(p.get("last_price") or 0) for p in legs if "FUT" in (p.get("tradingsymbol") or "")),
        None,
    )
    T = _time_to_expiry()
    net_delta = 0.0
    for p in legs:
        qty = p.get("quantity") or 0
        avg = float(p.get("average_price") or 0)
        ltp = float(p.get("last_price") or 0)
        pnl = float(p.get("pnl") or 0)
        sym = p.get("tradingsymbol") or ""
        delta_str = ""
        if qty != 0:
            if "FUT" in sym:
                pd = float(qty) * 1.0
                net_delta += pd
                delta_str = f"Δ={pd:+.2f}"
            else:
                m = re.search(r"(\d+)(CE|PE)$", sym)
                if m and fut_ltp:
                    K = float(m.group(1))
                    flag = "c" if m.group(2) == "CE" else "p"
                    ud = _unit_delta(ltp, fut_ltp, K, T, flag)
                    if ud is not None:
                        pd = qty * ud
                        net_delta += pd
                        delta_str = f"Δ={pd:+.2f}"
        src    = "(Q)" if p.get("_quote") else "   "
        spread = p.get("_spread")
        wide   = spread is not None and spread > WIDE_SPREAD
        wide_tag = f"  *** WIDE spread=₹{spread:.2f} ***" if wide else ""
        print(
            f"    {sym:30s} qty={qty:>4d}  "
            f"avg=₹{avg:7.2f}  ltp=₹{ltp:7.2f}{src}  pnl=₹{pnl:>+10.2f}  {delta_str}{wide_tag}"
        )
    net_delta_inr = net_delta * LOT_SIZE
    print(f"    NET Δ={net_delta:+.3f}  (≈₹{net_delta_inr:.0f} per ₹1 NG move)")

    if adx_info is not None:
        adx     = adx_info.get("adx")
        band    = adx_info.get("band", HEDGE_BAND_MILD)
        regime  = adx_info.get("regime", "UNKNOWN")
        adx_str = f"{adx:.1f}" if adx is not None else "n/a"
        delta_abs = abs(net_delta_inr)
        if delta_abs > band:
            hedge_tag = f"*** HEDGE NEEDED (₹{delta_abs:.0f} > band ₹{band}) ***"
        else:
            hedge_tag = f"OK  (₹{delta_abs:.0f} within band ₹{band})"
        print(f"    ADX={adx_str} {regime:<8s}  hedge_band=±₹{band}  delta=₹{net_delta_inr:+.0f}  → {hedge_tag}")

    return net_delta_inr


# ── order placement ──────────────────────────────────────────────────────────

def place_exits(kite, pairs: list[tuple]) -> list[dict]:
    """Place LIMIT exit for each (position, close_qty) pair."""
    results = []
    for p, close_qty in pairs:
        if close_qty <= 0:
            continue
        qty  = p.get("quantity") or 0
        side = kite.TRANSACTION_TYPE_BUY if qty < 0 else kite.TRANSACTION_TYPE_SELL
        ltp  = float(p.get("last_price") or 0)
        limit_px = (
            round_tick(ltp + 0.10) if qty < 0
            else round_tick(max(ltp - 0.10, TICK))
        )
        symbol = p["tradingsymbol"]
        try:
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=EXCHANGE,
                tradingsymbol=symbol,
                transaction_type=side,
                quantity=close_qty,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=limit_px,
                product=PRODUCT,
            )
            print(f"    [LIMIT] {symbol:30s} {side} qty={close_qty} @₹{limit_px:.2f}  oid={oid}")
            results.append({"symbol": symbol, "oid": oid, "qty": close_qty, "side": side})
        except Exception as exc:
            print(f"    [LIMIT-ERR] {symbol:30s} {exc}")
            results.append({"symbol": symbol, "oid": None, "qty": close_qty,
                            "side": side, "error": str(exc)})
        time.sleep(0.3)
    return results


def escalate_to_market(kite, results):
    try:
        orders = {o["order_id"]: o for o in kite.orders()}
    except Exception as exc:
        print(f"    [ORDERS-ERR] {exc}")
        return
    for r in results:
        oid = r.get("oid")
        if not oid:
            continue
        status = (orders.get(oid) or {}).get("status", "UNKNOWN")
        if status == "COMPLETE":
            print(f"    [FILLED-LIMIT] {r['symbol']:30s} oid={oid}")
            continue
        try:
            kite.modify_order(
                variety=kite.VARIETY_REGULAR,
                order_id=oid,
                order_type=kite.ORDER_TYPE_MARKET,
            )
            print(f"    [ESCALATE-MKT] {r['symbol']:30s} oid={oid} (was {status})")
        except Exception as exc:
            print(f"    [MODIFY-ERR] {r['symbol']:30s} oid={oid} {exc} → cancel+new MARKET")
            try:
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
            except Exception:
                pass
            try:
                new_oid = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=EXCHANGE,
                    tradingsymbol=r["symbol"],
                    transaction_type=r["side"],
                    quantity=r["qty"],
                    order_type=kite.ORDER_TYPE_MARKET,
                    product=PRODUCT,
                )
                print(f"    [NEW-MKT] {r['symbol']:30s} oid={new_oid}")
            except Exception as exc2:
                print(f"    [NEW-MKT-ERR] {r['symbol']:30s} {exc2}")


def fire_and_wait(kite, pairs, label):
    results = place_exits(kite, pairs)
    print(f"    waiting {LIMIT_WAIT_SECONDS}s for LIMIT fills…")
    time.sleep(LIMIT_WAIT_SECONDS)
    escalate_to_market(kite, results)


# ── actions ──────────────────────────────────────────────────────────────────

def do_half_exit(kite, legs, total_pnl):
    pairs = []
    for p in open_legs(legs):
        qty      = abs(p.get("quantity") or 0)
        exit_qty = math.ceil(qty / 2)
        pairs.append((p, exit_qty))
    print(f"    *** TRIGGER HALF-EXIT: M2M ₹{total_pnl:.2f} >= ₹{HALF_EXIT_THRESHOLD:.0f} ***")
    for p, eq in pairs:
        print(f"        {p.get('tradingsymbol'):30s}  full={abs(p.get('quantity') or 0)}  exit={eq}")
    fire_and_wait(kite, pairs, "half-exit")


def do_increment_exit(kite, legs, total_pnl, increment_n):
    threshold = HALF_EXIT_THRESHOLD + increment_n * INCREMENT_INR
    print(
        f"    *** TRIGGER INCREMENT-{increment_n}: M2M ₹{total_pnl:.2f} >= ₹{threshold:.0f}"
        f" — EXIT 1 LOT EACH ***"
    )
    pairs = [(p, 1) for p in open_legs(legs)]
    fire_and_wait(kite, pairs, f"increment-{increment_n}")


# ── single check ─────────────────────────────────────────────────────────────

def check_once() -> tuple[float, int]:
    """Run one M2M check + ladder action. Returns (total_pnl, exit_code)."""
    try:
        kite = get_session_manager().get_kite()
    except TokenStaleError as e:
        print(f"[{now_ist():%Y-%m-%d %H:%M:%S IST}] SESSION_INVALID reason={e}")
        return 0.0, 2

    try:
        legs = fetch_ng_legs(kite)
    except Exception as e:
        print(f"[{now_ist():%Y-%m-%d %H:%M:%S IST}] KITE_ERROR {e}")
        return 0.0, 3

    mark = get_mark_prices(kite, legs)
    if mark:
        legs = apply_mark_prices(legs, mark)

    total_pnl = round(sum(float(p.get("pnl") or 0) for p in legs), 2)
    state     = load_state()

    adx = fetch_adx(kite)
    band, regime = adx_hedge_band(adx)
    adx_info = {"adx": adx, "band": band, "regime": regime}

    print_state(legs, total_pnl, state, adx_info)

    if not open_legs(legs):
        print("    [ALL-FLAT] no open NG legs — nothing to do")
        return total_pnl, 0

    wide_legs = [
        p.get("tradingsymbol") for p in open_legs(legs)
        if (p.get("_spread") or 0) > WIDE_SPREAD
    ]
    if wide_legs:
        print(f"    [WIDE-SPREAD] exits blocked — wide spreads on: {', '.join(wide_legs)}")
        return total_pnl, 0

    nxt = next_threshold(state)
    if total_pnl < nxt:
        return total_pnl, 0

    if not state["half_exit_done"]:
        do_half_exit(kite, legs, total_pnl)
        state["half_exit_done"] = True
        state["half_exit_pnl"]  = total_pnl
        save_state(state)
        return total_pnl, 0

    n = state["increments_done"] + 1
    do_increment_exit(kite, legs, total_pnl, n)
    state["increments_done"]    = n
    state["last_increment_pnl"] = total_pnl
    save_state(state)
    return total_pnl, 0


# ── adaptive loop ─────────────────────────────────────────────────────────────

def run_loop() -> int:
    mode         = "NORMAL"
    fast_reason  = None   # "proximity" | "drop"
    prev_m2m     = None
    trough_m2m   = None
    stable_count = 0

    print(
        f"[{now_ist():%Y-%m-%d %H:%M:%S IST}] "
        f"[LOOP] started — NORMAL mode ({NORMAL_INTERVAL}s interval)"
    )

    while True:
        pnl, rc = check_once()

        if rc in (2, 3):
            time.sleep(FAST_INTERVAL)
            continue

        state = load_state()
        nxt   = next_threshold(state)

        # ── NORMAL → FAST ─────────────────────────────────────────────────
        if mode == "NORMAL":
            if pnl >= (1 - PROXIMITY_PCT) * nxt:
                mode, fast_reason, stable_count, trough_m2m = "FAST", "proximity", 0, pnl
                print(
                    f"    [MODE→FAST/proximity] ₹{pnl:.0f} >= "
                    f"{(1-PROXIMITY_PCT)*100:.0f}% of trigger ₹{nxt:.0f}"
                )
            elif (
                prev_m2m is not None
                and abs(prev_m2m) >= MIN_M2M_FOR_DROP
                and (prev_m2m - pnl) / abs(prev_m2m) > DROP_PCT
            ):
                drop_pct = (prev_m2m - pnl) / abs(prev_m2m) * 100
                mode, fast_reason, stable_count, trough_m2m = "FAST", "drop", 0, pnl
                print(
                    f"    [MODE→FAST/drop] M2M fell {drop_pct:.1f}% "
                    f"(₹{prev_m2m:.0f} → ₹{pnl:.0f})"
                )

        # ── FAST → NORMAL ─────────────────────────────────────────────────
        else:
            trough_m2m = min(trough_m2m, pnl) if trough_m2m is not None else pnl

            # stable tick: |ΔM2M| <= STABILIZE_PCT * trigger
            if prev_m2m is not None and abs(pnl - prev_m2m) / abs(nxt) <= STABILIZE_PCT:
                stable_count += 1
            else:
                stable_count = 0

            exit_fast = False

            if fast_reason == "proximity":
                if pnl >= (1 - PROXIMITY_PCT) * nxt:
                    stable_count = 0  # still in proximity zone — reset stability
                elif stable_count >= STABILIZE_COUNT:
                    exit_fast = True
                    print(
                        f"    [MODE→NORMAL] left proximity zone, "
                        f"stable {STABILIZE_COUNT}× 1-min"
                    )

            elif fast_reason == "drop":
                # recovery: M2M climbed 20%+ of trigger above trough
                if trough_m2m is not None and (pnl - trough_m2m) >= RECOVERY_PCT * abs(nxt):
                    exit_fast = True
                    print(
                        f"    [MODE→NORMAL] recovered ₹{pnl - trough_m2m:.0f} "
                        f"from trough ₹{trough_m2m:.0f}"
                    )
                elif stable_count >= STABILIZE_COUNT:
                    exit_fast = True
                    print(
                        f"    [MODE→NORMAL] drop stabilised, "
                        f"stable {STABILIZE_COUNT}× 1-min"
                    )

            if exit_fast:
                mode, fast_reason, stable_count, trough_m2m = "NORMAL", None, 0, None
                # immediately re-enter FAST if still near trigger
                if pnl >= (1 - PROXIMITY_PCT) * nxt:
                    mode, fast_reason, stable_count, trough_m2m = "FAST", "proximity", 0, pnl
                    print(f"    [MODE→FAST/proximity] still near trigger after normalise")

        prev_m2m   = pnl
        pre_eve    = is_pre_evening()
        interval   = FAST_INTERVAL if (mode == "FAST" or pre_eve) else NORMAL_INTERVAL
        reason_tag = fast_reason or ("pre-evening" if pre_eve else "-")
        print(
            f"    [LOOP] mode={mode:<6s}  reason={reason_tag:<11s}  "
            f"stable={stable_count:>2d}/{STABILIZE_COUNT}  sleep={interval}s"
        )
        time.sleep(interval)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="NG M2M ladder monitor")
    ap.add_argument(
        "--loop", action="store_true",
        help=f"Adaptive loop: {NORMAL_INTERVAL}s normal / {FAST_INTERVAL}s fast",
    )
    args = ap.parse_args()
    if args.loop:
        return run_loop()
    pnl, rc = check_once()
    return rc


if __name__ == "__main__":
    sys.exit(main())
