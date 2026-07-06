"""BANKNIFTY reversal monitor — buys 1 lot ATM PE on bearish reversal, trailing SL ₹2,000.

State machine (persisted at /tmp/bnf_reversal_state.json):
  WATCHING -> ENTERED (on reversal trigger)
  ENTERED  -> EXITED  (on trailing SL hit)
  EXITED   -> WATCHING (next day; auto-reset on date change)

Reversal trigger: spot drops >= REVERSAL_DROP_PCT below session high.
Trailing SL: exits when LTP drops >= TRAILING_RUPEES below max premium reached after entry.

One-shot per day. Re-entry blocked until next morning.
"""
from __future__ import annotations

import json
import os
import sys
import time as _t
import zoneinfo
from datetime import date, datetime, timezone

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

sys.path.insert(0, "/app")

from sqlalchemy.orm import sessionmaker
from app.kite_session import TokenStaleError, get_session_manager
from app.storage import init_db, Instrument

STATE_FILE = "/tmp/bnf_reversal_state.json"
REVERSAL_DROP_PCT = 0.0030  # 0.30% below session high
TRAILING_RUPEES = 2000.0  # ₹ from max premium
LOT_SIZE = 30  # BNF current
ENTRY_WINDOW_START = "09:20"
ENTRY_WINDOW_END = "15:00"


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)


def in_entry_window() -> bool:
    now = datetime.now(IST)
    hm = now.strftime("%H:%M")
    return ENTRY_WINDOW_START <= hm <= ENTRY_WINDOW_END


def get_atm_pe(db, kite, spot: float):
    atm = round(spot / 100) * 100
    today = datetime.now(IST).date()
    pe = (
        db.query(Instrument)
        .filter(
            Instrument.name == "BANKNIFTY",
            Instrument.instrument_type == "PE",
            Instrument.strike == atm,
            Instrument.expiry >= today,
        )
        .order_by(Instrument.expiry)
        .first()
    )
    return pe, atm


def fetch_ltp(kite, exchange: str, symbol: str) -> float | None:
    try:
        q = kite.ltp([f"{exchange}:{symbol}"])
        return float(q[f"{exchange}:{symbol}"]["last_price"])
    except Exception:
        return None


def limit_first_order(kite, symbol: str, side: str, qty: int, exchange: str = "NFO") -> dict:
    """Place LIMIT at touch, escalate to MARKET on drift or 60s timeout."""
    q0 = kite.quote([f"{exchange}:{symbol}"])[f"{exchange}:{symbol}"]
    bid = (q0.get("depth", {}).get("buy") or [{}])[0].get("price", 0)
    ask = (q0.get("depth", {}).get("sell") or [{}])[0].get("price", 0)
    limit_px = bid if side == "SELL" else ask
    if limit_px <= 0:
        try:
            oid = kite.place_order(
                variety="regular", exchange=exchange, tradingsymbol=symbol,
                transaction_type=side, quantity=qty, product="NRML", order_type="MARKET",
            )
        except Exception as e:
            msg = str(e)
            if "Markets are closed" in msg or "After Market Order" in msg or "AMO" in msg:
                return {"order_id": None, "status": "MARKET_CLOSED", "type": "DEFERRED"}
            raise
        _t.sleep(3)
        orders = kite.orders()
        this = next((x for x in orders if x.get("order_id") == oid), None)
        return {"order_id": oid, "status": this.get("status") if this else "UNKNOWN",
                "avg": float(this.get("average_price") or 0) if this else 0, "type": "MARKET_FALLBACK"}

    try:
        oid = kite.place_order(
            variety="regular", exchange=exchange, tradingsymbol=symbol,
            transaction_type=side, quantity=qty, product="NRML",
            order_type="LIMIT", price=limit_px,
        )
    except Exception as e:
        msg = str(e)
        if "Markets are closed" in msg or "After Market Order" in msg or "AMO" in msg:
            return {"order_id": None, "status": "MARKET_CLOSED", "type": "DEFERRED"}
        raise
    for check in [20, 40, 60]:
        _t.sleep(20)
        orders = kite.orders()
        this = next((x for x in orders if x.get("order_id") == oid), None)
        if not this:
            continue
        status = this.get("status")
        if status == "COMPLETE":
            return {"order_id": oid, "status": "COMPLETE",
                    "avg": float(this.get("average_price") or 0), "type": "LIMIT"}
        if status in ("REJECTED", "CANCELLED"):
            return {"order_id": oid, "status": status,
                    "msg": this.get("status_message"), "type": "LIMIT"}
        qN = kite.quote([f"{exchange}:{symbol}"])[f"{exchange}:{symbol}"]
        bidN = (qN.get("depth", {}).get("buy") or [{}])[0].get("price", 0)
        askN = (qN.get("depth", {}).get("sell") or [{}])[0].get("price", 0)
        moved_away = (bidN < limit_px) if side == "SELL" else (askN > limit_px)
        if moved_away or check >= 60:
            kite.modify_order(variety="regular", order_id=oid, order_type="MARKET")
            _t.sleep(5)
            orders = kite.orders()
            this2 = next((x for x in orders if x.get("order_id") == oid), None)
            return {"order_id": oid, "status": this2.get("status") if this2 else "UNKNOWN",
                    "avg": float(this2.get("average_price") or 0) if this2 else 0,
                    "type": "ESCALATED_MARKET"}
    return {"order_id": oid, "status": "FALLTHROUGH", "type": "LIMIT"}


def main() -> int:
    try:
        engine = init_db()
        Sess = sessionmaker(bind=engine)
        db = Sess()
        kite = get_session_manager().get_kite()
    except TokenStaleError as e:
        print(f"BNF_STATE=SESSION_INVALID reason={e}")
        return 2
    except Exception as e:
        print(f"BNF_STATE=KITE_ERROR reason={e}")
        return 3

    # spot
    spot = fetch_ltp(kite, "NSE", "NIFTY BANK")
    if not spot:
        print("BNF_STATE=NO_SPOT")
        return 1

    state = load_state()
    today_iso = datetime.now(IST).date().isoformat()

    # Reset on new day
    if state.get("session_date") != today_iso:
        state = {"state": "WATCHING", "session_high": spot, "session_date": today_iso}
        save_state(state)

    # Update session high (only when watching)
    if state["state"] == "WATCHING":
        if spot > state.get("session_high", 0):
            state["session_high"] = spot

    print(f"BNF_SPOT={spot:.2f} STATE={state['state']} SESSION_HIGH={state.get('session_high', 0):.2f}")

    # WATCHING — check for reversal trigger
    if state["state"] == "WATCHING":
        if not in_entry_window():
            print(f"BNF_INFO out_of_entry_window ({ENTRY_WINDOW_START}-{ENTRY_WINDOW_END})")
            save_state(state)
            return 0
        high = state["session_high"]
        drop_pct = (high - spot) / high if high > 0 else 0.0
        drop_thr = REVERSAL_DROP_PCT
        print(f"BNF_DROP_FROM_HIGH={drop_pct*100:.3f}% threshold={drop_thr*100:.2f}%")
        if drop_pct >= drop_thr:
            print(f"BNF_TRIGGER reversal_detected drop={drop_pct*100:.3f}%")
            pe, atm = get_atm_pe(db, kite, spot)
            if not pe:
                print(f"BNF_ERROR no ATM PE found for strike={atm}")
                save_state(state)
                return 1
            print(f"BNF_BUY {pe.tradingsymbol} qty={LOT_SIZE}")
            r = limit_first_order(kite, pe.tradingsymbol, "BUY", LOT_SIZE)
            print(f"BNF_ORDER {r}")
            if r["status"] == "COMPLETE":
                state["state"] = "ENTERED"
                state["pe_symbol"] = pe.tradingsymbol
                state["atm_strike"] = atm
                state["entry_premium"] = r["avg"]
                state["max_premium"] = r["avg"]
                state["entry_order_id"] = r["order_id"]
                state["entry_time"] = datetime.now(IST).isoformat()
                print(f"BNF_ENTERED premium={r['avg']} atm={atm}")
            else:
                print(f"BNF_ORDER_FAILED {r}")
        save_state(state)
        return 0

    # ENTERED — track trailing SL
    if state["state"] == "ENTERED":
        pe_sym = state["pe_symbol"]
        ltp = fetch_ltp(kite, "NFO", pe_sym)
        if not ltp:
            print(f"BNF_WARN no_ltp_for {pe_sym}")
            save_state(state)
            return 0
        max_prem = state.get("max_premium", state["entry_premium"])
        if ltp > max_prem:
            max_prem = ltp
            state["max_premium"] = max_prem
        unreal_pnl = (ltp - state["entry_premium"]) * LOT_SIZE
        max_pnl = (max_prem - state["entry_premium"]) * LOT_SIZE
        drawdown_rs = (max_prem - ltp) * LOT_SIZE
        print(f"BNF_POS {pe_sym} entry={state['entry_premium']} ltp={ltp} max_prem={max_prem} unreal_pnl={unreal_pnl:+.0f} max_pnl={max_pnl:+.0f} dd_from_max={drawdown_rs:.0f}")
        if drawdown_rs >= TRAILING_RUPEES:
            print(f"BNF_TRAIL_HIT drawdown={drawdown_rs:.0f} >= {TRAILING_RUPEES:.0f} — exiting")
            r = limit_first_order(kite, pe_sym, "SELL", LOT_SIZE)
            print(f"BNF_EXIT_ORDER {r}")
            if r["status"] == "COMPLETE":
                realized = (r["avg"] - state["entry_premium"]) * LOT_SIZE
                state["state"] = "EXITED"
                state["exit_premium"] = r["avg"]
                state["realized_pnl"] = realized
                state["exit_time"] = datetime.now(IST).isoformat()
                print(f"BNF_EXITED realized_pnl={realized:+.0f}")
            elif r["status"] == "MARKET_CLOSED":
                print("BNF_DEFERRED markets_closed — will retry on next market open")
            else:
                print(f"BNF_EXIT_FAILED {r}")
        save_state(state)
        return 0

    # EXITED — report only
    if state["state"] == "EXITED":
        print(f"BNF_DONE entry={state.get('entry_premium')} exit={state.get('exit_premium')} pnl={state.get('realized_pnl')}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
