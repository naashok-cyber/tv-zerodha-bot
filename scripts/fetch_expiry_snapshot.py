#!/usr/bin/env python3
"""
CLI wrapper — fetch expiry-day options snapshot for one or all configured underlyings.

Core logic lives in app/expiry_snapshot.py (used by the APScheduler jobs too).

Usage (inside Docker or .venv, from project root):
  python scripts/fetch_expiry_snapshot.py                 # all underlyings
  python scripts/fetch_expiry_snapshot.py NIFTY           # single underlying
  python scripts/fetch_expiry_snapshot.py CRUDEOILM --force   # skip expiry-day check

Underlyings: NIFTY  BANKNIFTY  MIDCPNIFTY  NATURALGAS  CRUDEOILM
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from app.expiry_snapshot import UNDERLYINGS, fetch_snapshot
from app.kite_session import get_session_manager


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch expiry-day options snapshot")
    parser.add_argument(
        "underlying", nargs="?", default=None,
        help="NIFTY | BANKNIFTY | MIDCPNIFTY | NATURALGAS | CRUDEOILM (default: all)",
    )
    parser.add_argument("--force", action="store_true",
                        help="Fetch even if today is not expiry day")
    args = parser.parse_args()

    kite = get_session_manager().get_kite()

    if args.underlying:
        key = args.underlying.upper()
        if key not in UNDERLYINGS:
            print(f"Unknown underlying '{key}'. Choose from: {', '.join(UNDERLYINGS)}")
            sys.exit(1)
        targets = {key: UNDERLYINGS[key]}
    else:
        targets = UNDERLYINGS

    total = 0
    for underlying, cfg in targets.items():
        total += fetch_snapshot(kite, underlying, cfg, force=args.force)

    print(f"\n{'='*65}")
    print(f"  Grand total: {total:,} bars saved across {len(targets)} underlying(s)")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
