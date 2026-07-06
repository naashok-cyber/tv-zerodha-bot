"""Print NG M2M periodically; if net profit >= THRESHOLD, close ceil(qty/2)
of each open NG leg via LIMIT with 15s escalate-to-MARKET fallback.

One-shot: writes /tmp/ng_reduce_fired.marker after firing so subsequent runs
only print and never re-reduce.

Usage:
  docker exec tv-zerodha-bot-bot-1 python3 scripts/monitor_ng_m2m.py
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kite_session import TokenStaleError, get_session_manager

THRESHOLD_INR = 8000.0
MARKER_PATH = "/tmp/ng_reduce_fired.marker"
TICK = 0.05
EXCHANGE = "MCX"
PRODUCT = "NRML"
LIMIT_WAIT_SECONDS = 15


def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


def is_ng(symbol: str) -> bool:
    return "NATURALGAS" in symbol or "NATGAS" in symbol


def fetch_ng_legs(kite):
    positions = kite.positions()
    net = positions.get("net", []) or []
    ng = [p for p in net if is_ng(p.get("tradingsymbol") or "")]
    return [p for p in ng if (p.get("quantity") or 0) != 0 or (p.get("pnl") or 0) != 0]


def print_state(legs, total_pnl, marker_present):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    open_legs = sum(1 for p in legs if (p.get("quantity") or 0) != 0)
    marker_flag = "FIRED" if marker_present else "armed"
    print(f"[{ts}] NG_M2M=₹{total_pnl:.2f}  open_legs={open_legs}  "
          f"threshold=₹{THRESHOLD_INR:.0f}  reduce={marker_flag}")
    for p in legs:
        qty = p.get("quantity") or 0
        avg = float(p.get("average_price") or 0)
        ltp = float(p.get("last_price") or 0)
        pnl = float(p.get("pnl") or 0)
        print(f"    {p.get('tradingsymbol'):30s} qty={qty:>4d}  "
              f"avg=₹{avg:7.2f}  ltp=₹{ltp:7.2f}  pnl=₹{pnl:>+10.2f}")


def close_half(kite, legs):
    """Close ceil(|qty|/2) of each open leg. LIMIT-first, escalate to MARKET
    after LIMIT_WAIT_SECONDS if not COMPLETE. Returns list of order results."""
    results = []
    for p in legs:
        qty = p.get("quantity") or 0
        if qty == 0:
            continue
        close_qty = math.ceil(abs(qty) / 2)
        # Short → BUY back; long → SELL to close
        side = kite.TRANSACTION_TYPE_BUY if qty < 0 else kite.TRANSACTION_TYPE_SELL
        ltp = float(p.get("last_price") or 0)
        limit_px = round_tick(ltp + 0.10) if qty < 0 else round_tick(max(ltp - 0.10, TICK))
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
    """Check LIMIT order status; modify any non-COMPLETE to MARKET."""
    try:
        orders = {o["order_id"]: o for o in kite.orders()}
    except Exception as exc:
        print(f"    [ORDERS-ERR] {exc}")
        return
    for r in results:
        oid = r.get("oid")
        if not oid:
            continue
        o = orders.get(oid)
        status = (o or {}).get("status", "UNKNOWN")
        if status == "COMPLETE":
            print(f"    [FILLED-LIMIT] {r['symbol']:30s} oid={oid}")
            continue
        # Escalate: modify to MARKET
        try:
            kite.modify_order(
                variety=kite.VARIETY_REGULAR,
                order_id=oid,
                order_type=kite.ORDER_TYPE_MARKET,
            )
            print(f"    [ESCALATE-MKT] {r['symbol']:30s} oid={oid} (was {status})")
        except Exception as exc:
            # Fallback: cancel + fresh MARKET order
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


def main() -> int:
    try:
        kite = get_session_manager().get_kite()
    except TokenStaleError as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] SESSION_INVALID reason={e}")
        return 2

    try:
        legs = fetch_ng_legs(kite)
    except Exception as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] KITE_ERROR {e}")
        return 3

    total_pnl = round(sum(float(p.get("pnl") or 0) for p in legs), 2)
    marker_present = os.path.exists(MARKER_PATH)
    print_state(legs, total_pnl, marker_present)

    if marker_present:
        return 0

    if total_pnl < THRESHOLD_INR:
        return 0

    print(f"    *** TRIGGER: M2M ₹{total_pnl:.2f} >= ₹{THRESHOLD_INR:.0f} — reducing by half ***")
    open_legs = [p for p in legs if (p.get("quantity") or 0) != 0]
    results = close_half(kite, open_legs)

    print(f"    waiting {LIMIT_WAIT_SECONDS}s for LIMIT fills...")
    time.sleep(LIMIT_WAIT_SECONDS)
    escalate_to_market(kite, results)

    with open(MARKER_PATH, "w") as f:
        f.write(f"fired_at={datetime.now().isoformat()} m2m={total_pnl:.2f}\n")
    print(f"    marker written: {MARKER_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
