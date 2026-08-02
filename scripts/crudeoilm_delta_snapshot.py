"""One-shot: fetch live CRUDEOILM positions, per-leg delta, and M2M impact
for a +100 point move in CRUDEOILM front-month futures."""
from __future__ import annotations

from datetime import datetime, time as dtime

from app.config import IST, get_settings
from app.delta_hedge import _matches_underlying
from app.greeks import compute_delta
from app.kite_session import get_session_manager
from sqlalchemy.orm import sessionmaker

from app.storage import Instrument, init_db

UNDERLYING = "CRUDEOILM"
EXCHANGE = "MCX"
DF = 100.0  # points move in futures we're simulating


def main() -> None:
    settings = get_settings()
    lot_units = settings.MCX_LOT_UNITS[UNDERLYING]

    engine = init_db()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    kite = get_session_manager().get_kite()
    positions = kite.positions().get("net", []) or []

    opt_legs = [
        p for p in positions
        if p.get("exchange") == EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches_underlying(p.get("tradingsymbol") or "", UNDERLYING)
        and (p["tradingsymbol"].endswith("CE") or p["tradingsymbol"].endswith("PE"))
    ]
    fut_legs = [
        p for p in positions
        if p.get("exchange") == EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches_underlying(p.get("tradingsymbol") or "", UNDERLYING)
        and p["tradingsymbol"].endswith("FUT")
    ]

    if not opt_legs and not fut_legs:
        print(f"No open {UNDERLYING} positions.")
        return

    now = datetime.now(IST)
    with SessionLocal() as session:
        # Resolve options expiry & F from an instrument row
        futures_inst = (
            session.query(Instrument)
            .filter(
                Instrument.name == UNDERLYING,
                Instrument.instrument_type == "FUT",
                Instrument.exchange == EXCHANGE,
                Instrument.expiry != None,  # noqa: E711
            )
            .order_by(Instrument.expiry.asc())
            .first()
        )
        if futures_inst is None:
            print(f"No {UNDERLYING} futures instrument in DB.")
            return
        fut_full = f"{EXCHANGE}:{futures_inst.tradingsymbol}"

        leg_fulls = [f"{EXCHANGE}:{p['tradingsymbol']}" for p in opt_legs]
        ltps = kite.ltp([fut_full] + leg_fulls)
        F = float(ltps[fut_full]["last_price"])

        exp_h, exp_m = (int(x) for x in settings.SESSION_CLOSE_MCX.split(":"))
        # T is computed against options expiry, not futures'.
        opt_expiry = None
        if opt_legs:
            first_opt = session.query(Instrument).filter_by(
                tradingsymbol=opt_legs[0]["tradingsymbol"]
            ).first()
            if first_opt is not None:
                opt_expiry = first_opt.expiry

        T = None
        if opt_expiry is not None:
            expiry_dt = datetime.combine(opt_expiry, dtime(exp_h, exp_m), tzinfo=IST)
            T = (expiry_dt - now).total_seconds() / 31_557_600.0

        print(f"CRUDEOILM front-fut  : {futures_inst.tradingsymbol}  F=₹{F:,.2f}")
        if opt_expiry:
            print(f"Options expiry       : {opt_expiry}  T={T:.4f}y")
        print(f"Lot units            : {lot_units} bbl/lot")
        print(f"Sim move             : +{DF:.0f} pts in F")
        print()

        rows = []
        total_delta = 0.0
        total_m2m = 0.0

        for fp in fut_legs:
            qty = fp["quantity"]  # lots (Kite MCX)
            pos_delta = qty * 1.0 * lot_units
            m2m = pos_delta * DF
            total_delta += pos_delta
            total_m2m += m2m
            rows.append((fp["tradingsymbol"], qty, None, None, pos_delta, m2m))

        for p in opt_legs:
            sym = p["tradingsymbol"]
            qty = p["quantity"]  # lots
            inst = session.query(Instrument).filter_by(tradingsymbol=sym).first()
            if inst is None:
                print(f"  ! no instrument row for {sym} — skipping")
                continue
            ltp = float(ltps[f"{EXCHANGE}:{sym}"]["last_price"])
            res = compute_delta(inst, ltp, F, T or 0.0)
            if res.delta is None:
                print(f"  ! delta failed for {sym}: {res.rejection_reason}")
                continue
            pos_delta = qty * res.delta * lot_units
            m2m = pos_delta * DF
            total_delta += pos_delta
            total_m2m += m2m
            rows.append((sym, qty, res.delta, ltp, pos_delta, m2m))

    # Print
    print(f"{'symbol':<28} {'qty':>5} {'LTP':>9} {'δ/unit':>8} {'pos δ':>10} {'ΔM2M/+100':>12}")
    print("-" * 78)
    for sym, qty, d_unit, ltp, pos_d, m2m in rows:
        ltp_s = f"{ltp:>9,.2f}" if ltp is not None else " " * 9
        d_s = f"{d_unit:>+8.4f}" if d_unit is not None else "    FUT "
        print(f"{sym:<28} {qty:>+5d} {ltp_s} {d_s} {pos_d:>+10.2f} {m2m:>+12,.0f}")
    print("-" * 78)
    print(f"{'TOTAL':<28} {'':>5} {'':>9} {'':>8} {total_delta:>+10.2f} {total_m2m:>+12,.0f}")
    print()
    print(f"Net delta (bbl-equivalent) : {total_delta:+.2f}")
    print(f"M2M change for +₹{DF:.0f}/bbl move in F : ₹{total_m2m:+,.0f}")
    print(f"M2M change for -₹{DF:.0f}/bbl move in F : ₹{-total_m2m:+,.0f}")


if __name__ == "__main__":
    main()
