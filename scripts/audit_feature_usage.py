#!/usr/bin/env python3
"""Which /control features have actually been used, and when?

Run this on the VM against the live DB before deleting anything:

    docker compose exec bot python scripts/audit_feature_usage.py
    docker compose exec bot python scripts/audit_feature_usage.py --days 120

Every /control surface is either a *toggle* (a state override + a job) or a
*strategy* (rows in closed_trades / hedge_actions / alerts). This walks both
sides and prints, per feature: whether it is on right now, when it last did
something, and how many times it did it in the window.

"Last fired" is read from the artefact the feature leaves behind, never from a
log — logs rotate, rows don't. A feature that is OFF *and* has a blank last-fired
column is a deletion candidate; a feature that is ON but blank is a config that
has been quietly dead (worse: it still costs a quote-bucket slot every minute).

Read-only. Opens the same DATABASE_URL the bot uses; touches no Kite API.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import IST, get_settings  # noqa: E402
from app.storage import (  # noqa: E402
    Alert,
    ClosedTrade,
    HedgeAction,
    IvSnapshot,
    Order,
    OrderFlowBar,
    init_db,
)
from app import state  # noqa: E402

# ── feature table ────────────────────────────────────────────────────────────
# (label, is_on(settings) -> bool | None, evidence-kind, evidence-key)
#
# evidence kinds:
#   "sid"    — closed_trades / alerts whose strategy_id starts with the key
#   "hedge"  — hedge_actions rows with trigger in the key tuple
#   "iv"     — iv_snapshots (straddle-defense monitor heartbeat)
#   "ofbar"  — orderflow_bars
#   "state"  — no artefact table; toggle state only
FEATURES: list[tuple[str, str, str, object]] = [
    ("EMA Analyzer",            "ema_analyzer",        "sid",   "ema_analyzer"),
    ("Entry Wings",             "entry_wings",         "hedge", ("entry",)),
    ("Partial Booking",         "partial_booking",     "sid",   "partial_"),
    ("Scheduled Straddle",      "scheduled_straddle",  "sid",   "sched_straddle"),
    ("Window Straddle",         "window_straddle",     "sid",   "window_straddle"),
    ("Straddle Defense",        "straddle_defense",    "iv",    None),
    ("Defense wing hedges",     "straddle_defense",    "hedge", ("reactive", "prehedge", "manual")),
    ("Order Flow Recorder",     "orderflow",           "ofbar", None),
    ("Voice / Quick Trade",     "voice",               "sid",   "voice_"),
    ("Voice straddles",         "voice",               "sid",   "straddle_"),
    ("TradingView webhooks",    "always",              "sid",   "__tv__"),
]

# Hedge engines: state getter name → (.env attr, underlying substring)
HEDGE_ENGINES: list[tuple[str, str, str, str]] = [
    ("NG band hedge",        "is_ng_hedge_enabled",              "NG_DELTA_HEDGE_ENABLED",       "NATURALGAS"),
    ("NG F-move hedge",      "is_ng_fmove_hedge_enabled",        "NG_FMOVE_HEDGE_ENABLED",       "NATURALGAS"),
    ("CRUDEOILM band hedge", "is_crude_hedge_enabled",           "CRUDEOILM_HEDGE_ENABLED",      "CRUDEOILM"),
    ("CRUDEOILM F-move",     "is_crudeoilm_fmove_hedge_enabled", "CRUDEOILM_FMOVE_HEDGE_ENABLED", "CRUDEOILM"),
    ("SILVERM hedge",        "is_silverm_hedge_enabled",         "SILVERM_HEDGE_ENABLED",        "SILVERM"),
    # No /control toggle today — .env only. Listed so the audit can't miss a
    # cron that is quietly burning a quote slot with no UI to notice it.
    ("GOLDM hedge (env-only)", None,                             "GOLDM_HEDGE_ENABLED",          "GOLDM"),
    ("BNF hedge (env-only)",   None,                             "BNF_HEDGE_ENABLED",            "BANKNIFTY"),
]


def _ist(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return (dt.astimezone(IST) if dt.tzinfo else dt).strftime("%Y-%m-%d %H:%M")


def _age(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    ref = dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
    d = (now - ref).days
    return "today" if d <= 0 else f"{d}d ago"


def _sid_evidence(sess, prefix: str, since: datetime):
    """Last-seen + count for a strategy_id prefix, over alerts (intent) and
    closed_trades (outcome). Alerts are the better liveness signal: a strategy
    that fired and got blocked by a risk guard still wrote an Alert row."""
    if prefix == "__tv__":
        # Everything that is NOT one of the bot's own internal strategy ids.
        internal = ("ema_analyzer", "entry_wings", "sched_straddle",
                    "window_straddle", "voice_", "straddle_", "partial_")
        q = select(func.max(Alert.received_at), func.count(Alert.id))
        for p in internal:
            q = q.where(~Alert.strategy_id.like(f"{p}%"))
        last_all, _ = sess.execute(q).one()
        _, n = sess.execute(q.where(Alert.received_at >= since)).one()
        return last_all, n

    q = select(func.max(Alert.received_at), func.count(Alert.id)).where(
        Alert.strategy_id.like(f"{prefix}%")
    )
    last_all, _ = sess.execute(q).one()
    _, n = sess.execute(q.where(Alert.received_at >= since)).one()
    if last_all is None:
        # Fall back to closed_trades for strategies that never wrote an Alert.
        q2 = select(func.max(ClosedTrade.closed_at), func.count(ClosedTrade.id)).where(
            ClosedTrade.strategy_id.like(f"{prefix}%")
        )
        last_all, _ = sess.execute(q2).one()
        _, n = sess.execute(q2.where(ClosedTrade.closed_at >= since)).one()
    return last_all, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=90,
                    help="counting window in days (default 90)")
    args = ap.parse_args()

    settings = get_settings()
    state.load_overrides_from_disk()
    SessionLocal = sessionmaker(bind=init_db(settings.DATABASE_URL), expire_on_commit=False)
    now = datetime.now(IST)
    since = now - timedelta(days=args.days)

    rows: list[tuple[str, str, str, str, int]] = []

    with SessionLocal() as sess:
        for label, toggle, kind, key in FEATURES:
            if toggle == "always":
                on = "—"
            elif toggle == "voice":
                from app.voice.config import is_voice_enabled
                on = "ON" if is_voice_enabled() else "off"
            elif toggle == "scheduled_straddle":
                on = "ON" if settings.SCHEDULED_STRADDLE_ENABLED else "off"
            elif toggle == "window_straddle":
                on = "ON" if state.is_window_straddle_enabled() else "off"
            else:
                getter = getattr(state, f"is_{toggle}_enabled")
                env_default = {
                    "ema_analyzer": settings.EMA_ANALYZER_ENABLED,
                    "entry_wings": settings.STRADDLE_ENTRY_WINGS_ENABLED,
                    "partial_booking": settings.PARTIAL_BOOKING_ENABLED,
                    "straddle_defense": settings.STRADDLE_DEFENSE_ENABLED,
                    "orderflow": settings.ORDERFLOW_ENABLED,
                }[toggle]
                on = "ON" if getter(env_default) else "off"

            if kind == "sid":
                last, n = _sid_evidence(sess, str(key), since)
            elif kind == "hedge":
                q = select(func.max(HedgeAction.proposed_at), func.count(HedgeAction.id)).where(
                    HedgeAction.trigger.in_(list(key))
                )
                last, _ = sess.execute(q).one()
                _, n = sess.execute(q.where(HedgeAction.proposed_at >= since)).one()
            elif kind == "iv":
                q = select(func.max(IvSnapshot.at), func.count(IvSnapshot.id))
                last, _ = sess.execute(q).one()
                _, n = sess.execute(q.where(IvSnapshot.at >= since)).one()
            elif kind == "ofbar":
                q = select(func.max(OrderFlowBar.bucket_ts), func.count(OrderFlowBar.id))
                last, _ = sess.execute(q).one()
                _, n = sess.execute(q.where(OrderFlowBar.bucket_ts >= since)).one()
            else:
                last, n = None, 0

            rows.append((label, on, _ist(last), _age(last, now), n))

        # Hedge engines leave their trace as orders on the underlying, so match
        # on the tradingsymbol rather than a strategy id.
        for label, getter_name, env_attr, sym in HEDGE_ENGINES:
            if getter_name is None:
                on = "ON" if getattr(settings, env_attr, False) else "off"
            else:
                on = "ON" if getattr(state, getter_name)(getattr(settings, env_attr)) else "off"
            q = select(func.max(Order.placed_at), func.count(Order.id)).where(
                Order.tradingsymbol.like(f"{sym}%"), Order.dry_run.is_(False)
            )
            last, _ = sess.execute(q).one()
            _, n = sess.execute(q.where(Order.placed_at >= since)).one()
            rows.append((label, on, _ist(last), _age(last, now), n))

    w = max(len(r[0]) for r in rows) + 2
    print(f"\nFeature usage — window {args.days}d, as of {now:%Y-%m-%d %H:%M} IST")
    print(f"DB: {settings.DATABASE_URL}\n")
    print(f"{'feature':<{w}}{'state':<7}{'last activity':<18}{'age':<10}{'events':>7}")
    print("─" * (w + 42))
    for label, on, last, age, n in rows:
        flag = "  ← dead?" if (on != "ON" and last == "—") else (
            "  ← ON but silent" if (on == "ON" and n == 0) else "")
        print(f"{label:<{w}}{on:<7}{last:<18}{age:<10}{n:>7}{flag}")
    print("\nNote: hedge-engine rows count *live* orders on that underlying from "
          "any source, so a busy manual book can mask an idle hedge cron. Cross-check "
          "a suspicious row against the engine's state file in ./data before deleting.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
