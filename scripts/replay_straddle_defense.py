"""Replay recorded iv_snapshots through the straddle-defense trigger logic to
tune thresholds before trusting SEMI_AUTO/AUTO execution.

The 1-min monitor writes every straddle's MTM + mean leg IV to the
iv_snapshots table (30-day retention). This script re-runs those series
through the exact production functions (iv_rising / evaluate_straddle) across
a grid of drawdown triggers x IV-sample counts and reports, per combination:

  alerts   — total triggers fired across the window
  days     — distinct straddle-days that fired at least once
  med 1st  — median time-of-day of the first trigger (is it the 18:00 ramp?)
  extraDD  — median FURTHER drawdown after the first trigger to the day's
             worst point: what the hedge would have saved (gross, before wing
             cost). Small extraDD => trigger fires too late / too tight.

Run on the VM (needs the live DB):
  cd /opt/tv-zerodha-bot
  sudo docker compose exec bot python scripts/replay_straddle_defense.py
  sudo docker compose exec bot python scripts/replay_straddle_defense.py \
      --triggers 3000,5000,7000 --samples 2,3,4 --days 30
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import IST, Settings  # noqa: E402
from app.straddle_defense import evaluate_straddle  # noqa: E402
from app.storage import IvSnapshot  # noqa: E402


def _load_series(db_url: str, days: int) -> dict[tuple[str, str, str], list]:
    """(date, straddle_key, underlying) -> [(at, mtm, iv), ...] ascending."""
    engine = create_engine(db_url)
    cutoff = datetime.now(IST) - timedelta(days=days)
    out: dict[tuple[str, str, str], list] = {}
    with Session(engine) as s:
        rows = (
            s.query(IvSnapshot)
            .filter(IvSnapshot.at >= cutoff)
            .order_by(IvSnapshot.at)
            .all()
        )
        for r in rows:
            at = r.at if r.at.tzinfo else r.at.replace(tzinfo=IST)
            key = (at.date().isoformat(), r.straddle_key, r.underlying)
            out.setdefault(key, []).append((at, r.mtm, r.iv_pct))
    engine.dispose()
    return out


def _replay_one(series: list, settings: Settings) -> tuple[list, float | None]:
    """Replay one straddle-day; returns (trigger times, extra drawdown after
    the first trigger to the day's worst MTM)."""
    st = {"date": "replay", "prehedge_sent": False, "straddles": {}}
    fired: list[datetime] = []
    ivs: list[float] = []
    first_trigger_mtm: float | None = None
    min_mtm: float | None = None
    for at, mtm, iv in series:
        if iv is not None:
            ivs.append(iv)
        if mtm is None:
            continue
        min_mtm = mtm if min_mtm is None else min(min_mtm, mtm)
        dd = evaluate_straddle("k", mtm, ivs, settings, st, at)
        if dd is not None:
            fired.append(at)
            if first_trigger_mtm is None:
                first_trigger_mtm = mtm
    extra = None
    if first_trigger_mtm is not None and min_mtm is not None:
        extra = first_trigger_mtm - min_mtm   # further slide the hedge absorbs
    return fired, extra


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="sqlite:///data/bot.db")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--triggers", default="3000,4000,5000,6000,7000",
                    help="comma-separated ₹ drawdown triggers")
    ap.add_argument("--samples", default="2,3,4",
                    help="comma-separated consecutive rising-IV sample counts")
    ap.add_argument("--rearm", type=int, default=20)
    ap.add_argument("--max-alerts", type=int, default=2)
    args = ap.parse_args()

    data = _load_series(args.db, args.days)
    if not data:
        print(f"No iv_snapshots in the last {args.days} days — enable the "
              f"defense monitor at /control and let it record first.")
        return

    n_days = len({k[0] for k in data})
    print(f"Replaying {len(data)} straddle-day series over {n_days} trading "
          f"days ({args.days}-day window)\n")
    hdr = f"{'trigger':>8} {'ivN':>4} | {'alerts':>6} {'days':>5} {'med 1st':>8} {'extraDD':>9}"
    print(hdr)
    print("-" * len(hdr))

    for trig in [float(t) for t in args.triggers.split(",")]:
        for n in [int(x) for x in args.samples.split(",")]:
            settings = Settings(
                _env_file=None, KITE_API_KEY="replay", SECRET_KEY="",
                DASHBOARD_PASSWORD="", PBKDF2_ITERATIONS=1,
                STRADDLE_DEFENSE_DRAWDOWN_TRIGGER=trig,
                STRADDLE_DEFENSE_IV_SAMPLES=n,
                STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY=args.max_alerts,
                STRADDLE_DEFENSE_REARM_MINUTES=args.rearm,
            )
            total = 0
            firing_days: set[str] = set()
            firsts: list[int] = []
            extras: list[float] = []
            for (day, _key, _under), series in data.items():
                fired, extra = _replay_one(series, settings)
                total += len(fired)
                if fired:
                    firing_days.add(day)
                    firsts.append(fired[0].hour * 60 + fired[0].minute)
                    if extra is not None:
                        extras.append(extra)
            med_first = ("--:--" if not firsts else
                         f"{int(statistics.median(firsts)) // 60:02d}:"
                         f"{int(statistics.median(firsts)) % 60:02d}")
            med_extra = f"₹{statistics.median(extras):,.0f}" if extras else "—"
            print(f"{trig:>8,.0f} {n:>4} | {total:>6} {len(firing_days):>5} "
                  f"{med_first:>8} {med_extra:>9}")

    print("\nReading the grid: pick the loosest (trigger, ivN) that still "
          "catches the bad days with a healthy extraDD — that is premium the "
          "wings would have absorbed. If extraDD is small, the trigger fires "
          "after the damage is done; loosen ivN or tighten the trigger.")


if __name__ == "__main__":
    main()
