#!/usr/bin/env python3
"""
Fetch current CRUDEOILM IV and compute theoretical PE + CE premiums
for all strikes 6500-7900 (step 100) using Black-76.

Shows theoretical premium and 50% limit price (tick-rounded to 0.10).

Run from repo root:
  docker compose run --rm bot python scripts/crudeoilm_iv_premiums.py
"""
from __future__ import annotations
import sys, math
from datetime import datetime, date

sys.path.insert(0, ".")
from app.kite_session import get_session_manager
from app.greeks import implied_volatility_b76
from py_vollib.black import black as b76_price

UNDERLYING  = "CRUDEOILM"
STRIKES     = list(range(6500, 8000, 100))   # 6500, 6600, ..., 7900
RISK_FREE   = 0.065
TICK        = 0.10


def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


def find_opts(instr: list[dict], expiry: date) -> list[dict]:
    seen: set[int] = set()
    deduped = []
    for r in instr:
        if (
            r["name"] == UNDERLYING
            and r["expiry"] == expiry
            and r.get("segment") == "MCX-OPT"
            and r["instrument_token"] not in seen
        ):
            seen.add(r["instrument_token"])
            deduped.append(r)
    return deduped


def find_opt(opts: list[dict], strike: float, flag: str) -> dict | None:
    flag_upper = flag.upper()
    for o in opts:
        if abs(float(o.get("strike", 0)) - strike) < 0.01:
            ts = o.get("tradingsymbol", "")
            it = o.get("instrument_type", "").upper()
            if flag_upper in ts or it == flag_upper:
                return o
    return None


# ── Connect ───────────────────────────────────────────────────────────────────
kite = get_session_manager().get_kite()

print("=" * 70)
print(f"  {UNDERLYING} IV & Premium Scanner")
print("=" * 70)

# ── Fetch all instruments ─────────────────────────────────────────────────────
print("\n[1] Fetching MCX instruments...")
today = date.today()
instr = kite.instruments("MCX")

# ── Nearest live option expiry (skip today if expiring) ──────────────────────
opt_expiries = sorted({
    r["expiry"] for r in instr
    if r["name"] == UNDERLYING and r.get("segment") == "MCX-OPT"
})
# Prefer expiry with at least 1 full day remaining
opt_expiry = next(
    (e for e in opt_expiries if e > today),
    next((e for e in opt_expiries if e >= today), None),
)
if opt_expiry is None:
    print("ERROR: No CRUDEOILM option expiries found.")
    sys.exit(1)
print(f"  Option expiry   : {opt_expiry}")

# ── Nearest futures at or after the option expiry ────────────────────────────
futs = sorted(
    [r for r in instr if r["name"] == UNDERLYING and r["instrument_type"] == "FUT"],
    key=lambda x: x["expiry"],
)
if not futs:
    print("ERROR: No CRUDEOILM futures found.")
    sys.exit(1)

fut = next((f for f in futs if f["expiry"] >= opt_expiry), futs[-1])
print(f"  Futures contract: {fut['tradingsymbol']}  expiry={fut['expiry']}")

ltp_resp = kite.ltp([f"MCX:{fut['tradingsymbol']}"])
F = ltp_resp[f"MCX:{fut['tradingsymbol']}"]["last_price"]
print(f"  Futures LTP (F) : ₹{F:.2f}")

# ── Time to expiry ────────────────────────────────────────────────────────────
expiry_dt = datetime(opt_expiry.year, opt_expiry.month, opt_expiry.day, 23, 30)
now = datetime.now()
T = max((expiry_dt - now).total_seconds() / (365.25 * 24 * 3600), 1.0 / 8760)
print(f"  Time to expiry  : {T * 365.25:.1f} calendar days  ({T:.6f} yr)")

# ── Find ATM PE and back-solve IV ─────────────────────────────────────────────
print("\n[2] Back-solving IV from ATM PE...")
opts = find_opts(instr, opt_expiry)
print(f"  Options found   : {len(opts)}")

atm_strike = round(F / 100) * 100
iv: float | None = None
used_strike: float | None = None

for candidate_k in [atm_strike, atm_strike - 100, atm_strike + 100,
                    atm_strike - 200, atm_strike + 200]:
    pe_opt = find_opt(opts, float(candidate_k), "PE")
    if pe_opt is None:
        continue
    resp = kite.ltp([f"MCX:{pe_opt['tradingsymbol']}"])
    ltp = resp[f"MCX:{pe_opt['tradingsymbol']}"]["last_price"]
    if ltp <= 0:
        print(f"  {pe_opt['tradingsymbol']} LTP=0, trying next strike...")
        continue
    print(f"  Reference option: {pe_opt['tradingsymbol']}  LTP=₹{ltp:.2f}")
    iv = implied_volatility_b76(ltp, F, float(candidate_k), RISK_FREE, T, 'p')
    if iv is not None:
        used_strike = float(candidate_k)
        break
    print(f"  IV solver failed for {pe_opt['tradingsymbol']}, trying next...")

if iv is None or used_strike is None:
    print("ERROR: Could not derive IV from any ATM option. Market may be closed.")
    sys.exit(1)

print(f"  IV (from {int(used_strike)} PE) : {iv * 100:.2f}%")

# ── Compute premiums for all strikes ─────────────────────────────────────────
print(f"\n[3] Theoretical premiums — F={F:.2f}  IV={iv*100:.2f}%  T={T*365.25:.1f}d")
print()
print(f"  {'Strike':>7}  {'PE theory':>10}  {'PE 50%':>8}  {'CE theory':>10}  {'CE 50%':>8}  {'Moneyness'}")
print(f"  {'-'*7}  {'-'*10}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*12}")

for K in STRIKES:
    pe_th = b76_price('p', F, K, T, RISK_FREE, iv)
    ce_th = b76_price('c', F, K, T, RISK_FREE, iv)
    pe_50 = round_tick(pe_th * 0.50)
    ce_50 = round_tick(ce_th * 0.50)

    diff = (K - F) / F * 100
    if diff < -1.5:
        moneyness = f"OTM {abs(diff):.1f}%"
    elif diff > 1.5:
        moneyness = f"ITM {diff:.1f}%"
    else:
        moneyness = "~ATM"

    atm_marker = " ◀" if abs(K - atm_strike) < 1 else ""
    print(
        f"  {K:>7}  {pe_th:>10.2f}  {pe_50:>8.2f}  {ce_th:>10.2f}  {ce_50:>8.2f}  {moneyness}{atm_marker}"
    )

print()
print(f"  F={F:.2f}  IV={iv*100:.2f}%  r={RISK_FREE*100:.1f}%  T={T*365.25:.1f}d  tick={TICK}")
print("\nDone.")
