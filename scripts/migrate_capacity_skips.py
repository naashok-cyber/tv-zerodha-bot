"""One-off: relabel historical capacity skips as SKIPPED_CAPACITY.

Before the SKIPPED_CAPACITY status existed, a run stopped by the capacity gate
was recorded as SKIPPED_REGIME plus a "no room for a new trade" recommendation.
Both routes read the newest row, so on a book at the position cap the scheduled
cadence buried every real analysis within the hour.

Relabelling the run is enough to hide those rows everywhere: /runs/latest and
/recommendations both exclude runs whose status analysed nothing. Nothing is
deleted — the recommendation rows stay as the audit record of the skip.

    python3 scripts/migrate_capacity_skips.py            # report only
    python3 scripts/migrate_capacity_skips.py --apply    # write
"""
from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from app.commodity_agents.storage import AgentRun, CommodityRecommendation
from app.storage import init_db

_PREFIX = "Capacity gate:"


def main(apply: bool) -> int:
    engine = init_db()
    with Session(engine) as session:
        rows = (
            session.query(AgentRun, CommodityRecommendation)
            .join(CommodityRecommendation,
                  CommodityRecommendation.run_pk == AgentRun.id)
            .filter(AgentRun.status == "SKIPPED_REGIME",
                    CommodityRecommendation.reasoning_summary.like(_PREFIX + "%"))
            .order_by(AgentRun.started_at.desc())
            .all()
        )
        print(f"{len(rows)} capacity-gate runs mislabelled as SKIPPED_REGIME")
        for run, rec in rows[:15]:
            print(f"  #{run.id:>5} {run.commodity:<11} {run.started_at:%Y-%m-%d %H:%M}"
                  f"  {(rec.reasoning_summary or '')[:80]}")
        if len(rows) > 15:
            print(f"  ... and {len(rows) - 15} more")
        if not apply:
            print("\nreport only — rerun with --apply to write")
            return 0
        for run, rec in rows:
            run.status = "SKIPPED_CAPACITY"
            if not run.error:
                run.error = rec.reasoning_summary
        session.commit()
        print(f"\nrelabelled {len(rows)} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
