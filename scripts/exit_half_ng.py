"""Exit ~55% of NG book to land net delta at slightly-positive (~+305 mmBtu).

Targets (2026-07-21 evening, computed with F=276.90, T=3.5d):
    BUY  3 NATURALGAS26JUL270CE  (was -7 → -4)
    BUY  3 NATURALGAS26JUL275CE  (was -5 → -2)
    SELL 2 NATURALGAS26JUL275PE  (was +3 → +1)
    BUY  9 NATURALGAS26JUL280PE  (was -17 → -8)
    BUY  1 NATURALGAS26JULFUT    (was -1 →  0)

LIMIT at best opposite-book price + one tick; escalate unfilled to MARKET
after 15s. Uses each instrument's own tick_size (option 0.05, FUT 0.10).
"""
import sys
import time
from decimal import Decimal

sys.path.insert(0, "/app")

from sqlalchemy.orm import Session

from app.kite_session import get_session_manager
from app.storage import init_db, Instrument


EXCHANGE = "MCX"
PRODUCT  = "NRML"
LIMIT_WAIT_SEC = 15

TARGETS = [
    ("NATURALGAS26JUL270CE", "BUY",  3),
    ("NATURALGAS26JUL275CE", "BUY",  3),
    ("NATURALGAS26JUL275PE", "SELL", 2),
    ("NATURALGAS26JUL280PE", "BUY",  9),
    ("NATURALGAS26JULFUT",   "BUY",  1),
]


def round_tick(px: float, tick: float) -> float:
    steps = round(px / tick)
    return round(steps * tick, 2)


def main() -> None:
    kite = get_session_manager().get_kite()
    engine = init_db()

    positions = {p["tradingsymbol"]: p for p in kite.positions().get("net", [])}

    # ── Pre-flight: validate each target against live position + fetch tick ──
    plan = []  # (symbol, side, qty, tick, current_qty, ltp)
    with Session(engine) as sess:
        for sym, side, qty in TARGETS:
            p = positions.get(sym)
            if not p:
                print(f"  [SKIP] {sym} not in current positions")
                continue
            cur_qty = int(p.get("quantity") or 0)
            if cur_qty == 0:
                print(f"  [SKIP] {sym} qty=0 (already flat)")
                continue
            # side sanity: BUY only against a short, SELL only against a long
            if side == "BUY" and cur_qty >= 0:
                print(f"  [ABORT] {sym} plan={side} but current qty={cur_qty:+d} (not short)")
                sys.exit(1)
            if side == "SELL" and cur_qty <= 0:
                print(f"  [ABORT] {sym} plan={side} but current qty={cur_qty:+d} (not long)")
                sys.exit(1)
            # size sanity: don't over-close
            if qty > abs(cur_qty):
                print(f"  [ABORT] {sym} plan={side} {qty} exceeds |current qty|={abs(cur_qty)}")
                sys.exit(1)

            inst = sess.query(Instrument).filter_by(tradingsymbol=sym).first()
            if inst is None:
                print(f"  [ABORT] {sym} not in Instrument table")
                sys.exit(1)
            tick = float(inst.tick_size or 0.05)
            ltp  = float(p.get("last_price") or 0)
            plan.append((sym, side, qty, tick, cur_qty, ltp))
            print(f"  [PLAN] {sym:30s} cur={cur_qty:+3d} → {side} {qty}  ltp={ltp:.2f} tick={tick}")

    if not plan:
        print("\nNothing to do.")
        return

    # ── Place LIMITs ──
    print("\n--- placing LIMITs ---")
    results = []
    for sym, side, qty, tick, cur_qty, ltp in plan:
        # BUY hits the ask, SELL hits the bid. Fetch depth for a real price;
        # fall back to LTP ± 1 tick to be marketable if depth is missing.
        try:
            q = kite.quote([f"{EXCHANGE}:{sym}"])[f"{EXCHANGE}:{sym}"]
            depth = q.get("depth") or {}
            book  = depth.get("sell" if side == "BUY" else "buy") or []
            top   = float(book[0]["price"]) if book and book[0].get("price") else 0.0
        except Exception as exc:
            print(f"    [QUOTE-ERR] {sym}: {exc}")
            top = 0.0

        if top > 0:
            # Add one tick of slack in the right direction so we're marketable
            raw_px = top + tick if side == "BUY" else top - tick
        else:
            raw_px = ltp + tick if side == "BUY" else ltp - tick
        px = round_tick(max(raw_px, tick), tick)

        try:
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=EXCHANGE,
                tradingsymbol=sym,
                transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=px,
                product=PRODUCT,
            )
            print(f"    [LIMIT] {sym:30s} {side} qty={qty} @ ₹{px:.2f}  oid={oid}")
            results.append({"symbol": sym, "side": side, "qty": qty, "oid": oid})
        except Exception as exc:
            print(f"    [LIMIT-ERR] {sym:30s} {side} qty={qty}: {exc}")
            results.append({"symbol": sym, "side": side, "qty": qty, "oid": None, "error": str(exc)})
        time.sleep(0.3)

    # ── Wait then escalate unfilled ──
    print(f"\n--- waiting {LIMIT_WAIT_SEC}s for LIMIT fills ---")
    time.sleep(LIMIT_WAIT_SEC)

    print("\n--- escalating unfilled → MARKET ---")
    try:
        order_map = {o["order_id"]: o for o in kite.orders()}
    except Exception as exc:
        print(f"    [ORDERS-ERR] {exc}")
        order_map = {}

    for r in results:
        oid = r.get("oid")
        if not oid:
            continue
        status = (order_map.get(oid) or {}).get("status", "UNKNOWN")
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
                    transaction_type=kite.TRANSACTION_TYPE_BUY if r["side"] == "BUY" else kite.TRANSACTION_TYPE_SELL,
                    quantity=r["qty"],
                    order_type=kite.ORDER_TYPE_MARKET,
                    product=PRODUCT,
                )
                print(f"    [NEW-MKT] {r['symbol']:30s} oid={new_oid}")
            except Exception as exc2:
                print(f"    [NEW-MKT-ERR] {r['symbol']:30s} {exc2}")

    # ── Final ──
    time.sleep(2)
    print("\n--- final NG positions ---")
    for p in kite.positions().get("net", []):
        if "NATURALGAS" in p["tradingsymbol"] and p["quantity"] != 0:
            print(f"  {p['tradingsymbol']:30s} qty={p['quantity']:+5d} avg={p['average_price']:.2f} ltp={p['last_price']:.2f}")


if __name__ == "__main__":
    main()
