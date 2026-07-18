"""One-shot: exit 50% of each NG short leg via LIMIT (auto-escalate to MARKET)."""
import sys, time
sys.path.insert(0, '/app')
sys.path.insert(0, '/app/scripts')

from app.kite_session import get_session_manager
from scripts.monitor_ng_m2m import place_exits, escalate_to_market

kite = get_session_manager().get_kite()

targets = {
    "NATURALGAS26JUL270CE": 1,
    "NATURALGAS26JUL275CE": 5,
    "NATURALGAS26JUL275PE": 2,
    "NATURALGAS26JUL280PE": 5,
}

positions = {p["tradingsymbol"]: p for p in kite.positions().get("net", [])}
pairs = []
for sym, qty in targets.items():
    p = positions.get(sym)
    if not p:
        print(f"[MISS] {sym} not in positions", flush=True)
        continue
    print(f"[PLAN] {sym} current_qty={p['quantity']} ltp={p['last_price']} → BUY {qty}", flush=True)
    pairs.append((p, qty))

print("\n--- placing LIMITs ---", flush=True)
results = place_exits(kite, pairs)

print(f"\n--- waiting 15s for LIMIT fills ---", flush=True)
time.sleep(15)

print("\n--- escalating unfilled → MARKET ---", flush=True)
escalate_to_market(kite, results)

print("\n--- final positions ---", flush=True)
for p in kite.positions().get("net", []):
    if "NATURALGAS" in p["tradingsymbol"] and p["quantity"] != 0:
        print(f"  {p['tradingsymbol']:30s} qty={p['quantity']:5d} avg={p['average_price']:.2f}", flush=True)
