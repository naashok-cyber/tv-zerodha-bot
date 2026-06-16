#!/usr/bin/env python3
"""
Place BUY LIMIT orders for CRUDEOILM CE strikes 7200-7900 (step 100)
at 50% of IV-derived theoretical premium. qty=5 per strike.

Run from repo root:
  docker compose run --rm -v $(pwd)/scripts:/app/scripts bot python scripts/place_crudeoilm_ce_ladder.py
"""
from __future__ import annotations
import sys, time
from datetime import datetime, date

sys.path.insert(0, ".")
from app.kite_session import get_session_manager
from app.greeks import implied_volatility_b76
from py_vollib.black import black as b76_price

UNDERLYING   = "CRUDEOILM"
STRIKES      = list(range(7200, 8000, 100))   # 7200..7900
QTY          = 5
RISK_FREE    = 0.065
TICK         = 0.10
EXCHANGE     = "MCX"
PRODUCT      = "NRML"


def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


kite = get_session_manager().get_kite()

print("=" * 70)
print(f"  CRUDEOILM CE Ladder — BUY LIMIT  ({len(STRIKES)} strikes × qty {QTY})")
print("=" * 70)

# ── Instruments ───────────────────────────────────────────────────────────────
print("\n[1] Fetching MCX instruments...")
today = date.today()
instr = kite.instruments(EXCHANGE)

opt_expiry = next(
    (
        e for e in sorted({
            r["expiry"] for r in instr
            if r["name"] == UNDERLYING and r.get("segment") == "MCX-OPT"
        })
        if e > today
    ),
    None,
)
if opt_expiry is None:
    print("ERROR: No live CRUDEOILM option expiry found.")
    sys.exit(1)
print(f"  Option expiry   : {opt_expiry}")

futs = sorted(
    [r for r in instr if r["name"] == UNDERLYING and r["instrument_type"] == "FUT"],
    key=lambda x: x["expiry"],
)
fut = next((f for f in futs if f["expiry"] >= opt_expiry), futs[-1])
print(f"  Futures contract: {fut['tradingsymbol']}  expiry={fut['expiry']}")

opts = {
    (r["instrument_type"], int(r["strike"])): r
    for r in instr
    if r["name"] == UNDERLYING
    and r["expiry"] == opt_expiry
    and r.get("segment") == "MCX-OPT"
}

# ── Futures LTP ───────────────────────────────────────────────────────────────
ltp_resp = kite.ltp([f"{EXCHANGE}:{fut['tradingsymbol']}"])
F = ltp_resp[f"{EXCHANGE}:{fut['tradingsymbol']}"]["last_price"]
print(f"  Futures LTP (F) : ₹{F:.2f}")

expiry_dt = datetime(opt_expiry.year, opt_expiry.month, opt_expiry.day, 23, 30)
now = datetime.now()
T = max((expiry_dt - now).total_seconds() / (365.25 * 24 * 3600), 1.0 / 8760)
print(f"  Time to expiry  : {T * 365.25:.1f} calendar days")

# ── Back-solve IV from ATM PE ─────────────────────────────────────────────────
print("\n[2] Back-solving IV from ATM PE...")
atm_strike = round(F / 100) * 100
iv: float | None = None

for candidate_k in [atm_strike, atm_strike - 100, atm_strike + 100,
                    atm_strike - 200, atm_strike + 200]:
    pe_instr = opts.get(("PE", int(candidate_k)))
    if pe_instr is None:
        continue
    resp = kite.ltp([f"{EXCHANGE}:{pe_instr['tradingsymbol']}"])
    ltp = resp[f"{EXCHANGE}:{pe_instr['tradingsymbol']}"]["last_price"]
    if ltp <= 0:
        continue
    iv = implied_volatility_b76(ltp, F, float(candidate_k), RISK_FREE, T, 'p')
    if iv is not None:
        print(f"  {pe_instr['tradingsymbol']} LTP=₹{ltp:.2f}  IV={iv*100:.2f}%")
        break

if iv is None:
    print("ERROR: IV solver failed. Market may be closed.")
    sys.exit(1)

# ── Build CE order list ───────────────────────────────────────────────────────
print(f"\n[3] Orders to be placed (BUY LIMIT CE, qty={QTY} each)")
print()
print(f"  {'Strike':>7}  {'Symbol':>28}  {'Theory':>8}  {'Limit ₹':>8}  {'Total ₹':>10}")
print(f"  {'-'*7}  {'-'*28}  {'-'*8}  {'-'*8}  {'-'*10}")

orders: list[dict] = []
for K in STRIKES:
    ce_instr = opts.get(("CE", K))
    if ce_instr is None:
        print(f"  {K:>7}  *** NOT FOUND IN INSTRUMENTS ***")
        continue
    theory = b76_price('c', F, K, T, RISK_FREE, iv)
    limit  = round_tick(theory * 0.50)
    total  = limit * QTY
    print(f"  {K:>7}  {ce_instr['tradingsymbol']:>28}  {theory:>8.2f}  {limit:>8.2f}  {total:>10.2f}")
    orders.append({
        "tradingsymbol": ce_instr["tradingsymbol"],
        "strike": K,
        "limit_price": limit,
    })

if not orders:
    print("\nERROR: No CE instruments found. Aborting.")
    sys.exit(1)

total_outlay = sum(o["limit_price"] * QTY for o in orders)
print(f"\n  {len(orders)} orders  |  Max outlay if all fill: ₹{total_outlay:,.2f}")

# ── Place orders ──────────────────────────────────────────────────────────────
print(f"\n[4] Placing {len(orders)} BUY LIMIT CE orders...")
results = []
for o in orders:
    try:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = EXCHANGE,
            tradingsymbol    = o["tradingsymbol"],
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = QTY,
            order_type       = kite.ORDER_TYPE_LIMIT,
            price            = o["limit_price"],
            product          = PRODUCT,
        )
        print(f"  {o['tradingsymbol']:30s}  limit=₹{o['limit_price']:.2f}  order_id={order_id}  OK")
        results.append({"strike": o["strike"], "order_id": order_id, "status": "placed"})
    except Exception as exc:
        print(f"  {o['tradingsymbol']:30s}  limit=₹{o['limit_price']:.2f}  ERROR: {exc}")
        results.append({"strike": o["strike"], "order_id": None, "status": f"ERROR: {exc}"})
    time.sleep(0.3)

print(f"\n{'='*70}")
placed = [r for r in results if r["order_id"]]
failed = [r for r in results if not r["order_id"]]
print(f"  Placed : {len(placed)}/{len(orders)}")
if failed:
    print(f"  Failed : {[r['strike'] for r in failed]}")
print(f"{'='*70}")
