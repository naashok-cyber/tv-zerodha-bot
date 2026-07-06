"""Print current NATURALGAS net P&L from open positions on the broker side.

Usage: python3 scripts/check_naturalgas_pnl.py

Output format (single line, machine-parseable):
  NG_PNL=<float> POSITIONS=<n> ALERT=<YES|NO>
followed by a per-leg breakdown.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kite_session import TokenStaleError, get_session_manager

LOSS_THRESHOLD_INR = 2000.0


def main() -> int:
    try:
        kite = get_session_manager().get_kite()
    except TokenStaleError as e:
        print(f"NG_PNL=NA POSITIONS=NA ALERT=SESSION_INVALID reason={e}")
        return 2

    try:
        positions = kite.positions()
    except Exception as e:
        print(f"NG_PNL=NA POSITIONS=NA ALERT=KITE_ERROR reason={e}")
        return 3

    net = positions.get("net", []) or []
    ng = [
        p for p in net
        if "NATURALGAS" in (p.get("tradingsymbol") or "")
        or "NATGAS" in (p.get("tradingsymbol") or "")
    ]
    # Only include legs that are currently open (non-zero net qty) OR closed today with realised P&L.
    ng_active = [p for p in ng if (p.get("quantity") or 0) != 0 or (p.get("pnl") or 0) != 0]

    total_pnl = sum(float(p.get("pnl") or 0) for p in ng_active)
    open_legs = sum(1 for p in ng_active if (p.get("quantity") or 0) != 0)
    alert = "YES" if total_pnl <= -LOSS_THRESHOLD_INR else "NO"

    print(f"NG_PNL={total_pnl:.2f} POSITIONS={open_legs} ALERT={alert}")
    if ng_active:
        print("Legs:")
        for p in ng_active:
            print(
                f"  {p.get('tradingsymbol')} qty={p.get('quantity')} "
                f"avg={p.get('average_price')} ltp={p.get('last_price')} "
                f"pnl={float(p.get('pnl') or 0):.2f}"
            )
    else:
        print("No NATURALGAS positions open and no realised P&L today.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
