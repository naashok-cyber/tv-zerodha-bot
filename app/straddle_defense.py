"""Straddle defense — Phase 1: monitor short straddles and alert. NEVER executes.

Every minute during MCX hours (cron in scheduler.py, gated live by the
/control toggle) the job:

  1. Computes per-straddle MTM + mean leg IV via compute_portfolio_greeks
     (one quote round, shared code path with /desk and /control).
  2. Samples both into the iv_snapshots table — the IV series is the
     expansion-confirmation signal; MTM feeds the drawdown trigger.
  3. Tracks each straddle's peak MTM for the day (persisted to
     data/straddle_defense_state.json so restarts don't blind the trigger).
  4. Fires a Telegram alert when BOTH hold:
       drawdown-from-peak >= STRADDLE_DEFENSE_DRAWDOWN_TRIGGER
       AND IV rose for STRADDLE_DEFENSE_IV_SAMPLES consecutive samples.
     Hysteresis: max STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY alerts per straddle
     and a STRADDLE_DEFENSE_REARM_MINUTES quiet period between them.
  5. Sends a once-daily pre-hedge reminder at STRADDLE_DEFENSE_PREHEDGE_TIME
     when short straddles exist (the cheap moment to buy wings, before the
     evening IV ramp).

Phase 2+ (scheduled wing placement, reactive execution, GTT coordination)
builds on the same evaluate/trigger functions — kept pure for testability.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any

from app import state
from app.config import IST

log = logging.getLogger(__name__)

_PREFIX = "[straddle_defense]"
_STATE_PATH = os.path.join("data", "straddle_defense_state.json")
_SNAPSHOT_RETENTION_DAYS = 30
_IV_SERIES_WINDOW = 60          # samples pulled for slope evaluation / card

_lock = threading.Lock()
_state: dict | None = None      # {"date": "YYYY-MM-DD", "prehedge_sent": bool,
                                #  "straddles": {key: {"peak": f, "alerts": n,
                                #                       "last_alert": iso|None}}}


# ── persisted day-state ───────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        with open(_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
    except OSError as exc:
        log.warning("%s could not persist state: %s", _PREFIX, exc)


def _today_state(now: datetime) -> dict:
    """Return the mutable day-state dict, resetting peaks on date change."""
    global _state
    with _lock:
        if _state is None:
            _state = _load_state()
        today = now.date().isoformat()
        if _state.get("date") != today:
            _state = {"date": today, "prehedge_sent": False, "straddles": {}}
        return _state


def reset_state_for_tests() -> None:
    global _state
    with _lock:
        _state = None


# ── pure trigger logic ────────────────────────────────────────────────────────

def iv_rising(samples: list[float], n: int) -> bool:
    """True when the last n deltas of the IV series are all positive
    (needs n+1 samples). One flat or falling step breaks the streak —
    this is the noise filter, not a trend model."""
    if n <= 0 or len(samples) < n + 1:
        return False
    tail = samples[-(n + 1):]
    return all(b > a for a, b in zip(tail, tail[1:]))


def evaluate_straddle(
    key: str,
    mtm: float,
    iv_series: list[float],
    settings: Any,
    st: dict,
    now: datetime,
) -> float | None:
    """Update the straddle's peak; return the drawdown when the reactive
    trigger fires (respecting hysteresis), else None. Mutates st."""
    rec = st["straddles"].setdefault(key, {"peak": mtm, "alerts": 0, "last_alert": None})
    if mtm > rec["peak"]:
        rec["peak"] = mtm
    drawdown = rec["peak"] - mtm

    if drawdown < settings.STRADDLE_DEFENSE_DRAWDOWN_TRIGGER:
        return None
    if not iv_rising(iv_series, settings.STRADDLE_DEFENSE_IV_SAMPLES):
        return None
    if rec["alerts"] >= settings.STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY:
        return None
    if rec["last_alert"] is not None:
        last = datetime.fromisoformat(rec["last_alert"])
        if (now - last).total_seconds() < settings.STRADDLE_DEFENSE_REARM_MINUTES * 60:
            return None

    rec["alerts"] += 1
    rec["last_alert"] = now.isoformat()
    return drawdown


# ── the 1-min job ─────────────────────────────────────────────────────────────

def straddle_defense_job(settings: Any, session_factory: Any) -> None:
    if not state.is_straddle_defense_enabled(settings.STRADDLE_DEFENSE_ENABLED):
        return

    from app.commodity_agents.notify import send_telegram
    from app.commodity_agents.portfolio import compute_portfolio_greeks
    from app.kite_session import get_session_manager
    from app.storage import IvSnapshot

    now = datetime.now(IST)
    try:
        kite = get_session_manager().get_kite()
    except Exception:
        return  # no session — nothing to monitor

    try:
        with session_factory() as session:
            greeks = compute_portfolio_greeks(session, kite, settings, now=now)
            shorts = {k: g for k, g in (greeks.get("straddles") or {}).items()
                      if g.get("short")}
            st = _today_state(now)

            for key, grp in shorts.items():
                mtm = grp.get("mtm")
                iv = grp.get("iv_mean_pct")

                iv_series = [
                    v for (v,) in session.query(IvSnapshot.iv_pct)
                    .filter(IvSnapshot.straddle_key == key,
                            IvSnapshot.at >= now.replace(hour=0, minute=0, second=0,
                                                         microsecond=0),
                            IvSnapshot.iv_pct != None)  # noqa: E711
                    .order_by(IvSnapshot.at.desc())
                    .limit(_IV_SERIES_WINDOW).all()
                ][::-1]
                if iv is not None:
                    iv_series.append(iv)

                session.add(IvSnapshot(at=now, underlying=grp["underlying"],
                                       straddle_key=key, mtm=mtm, iv_pct=iv))

                if mtm is None:
                    continue
                drawdown = evaluate_straddle(key, mtm, iv_series, settings, st, now)
                if drawdown is not None:
                    rec = st["straddles"][key]
                    text = (
                        f"⚠️ <b>{grp['underlying']} straddle defense</b>\n"
                        f"Drawdown ₹{drawdown:,.0f} from peak "
                        f"(peak ₹{rec['peak']:,.0f} → now ₹{mtm:,.0f}) "
                        f"with IV rising ({iv:.1f}%).\n"
                        f"Legs: {', '.join(grp['legs'])}\n"
                        f"Consider buying OTM wings against the shorts. "
                        f"Alert {rec['alerts']}/{settings.STRADDLE_DEFENSE_MAX_ALERTS_PER_DAY} today."
                    )
                    send_telegram(settings, text)
                    log.warning("%s ALERT %s drawdown=%.0f iv=%.2f", _PREFIX,
                                grp["underlying"], drawdown, iv or -1)

            # once-daily pre-hedge reminder — the cheap moment to buy wings.
            # Only fires within 60 min of the window so enabling the toggle
            # late in the evening doesn't send a stale reminder.
            pre = settings.STRADDLE_DEFENSE_PREHEDGE_TIME
            in_window = False
            if pre:
                try:
                    ph, pm = pre.split(":")
                    pre_min = int(ph) * 60 + int(pm)
                    now_min = now.hour * 60 + now.minute
                    in_window = pre_min <= now_min < pre_min + 60
                except ValueError:
                    in_window = False
            if shorts and in_window and not st["prehedge_sent"]:
                st["prehedge_sent"] = True
                names = ", ".join(sorted({g["underlying"] for g in shorts.values()}))
                send_telegram(settings, (
                    f"\U0001f552 <b>Pre-hedge window</b> ({pre} IST)\n"
                    f"Short straddles open: {names}. Evening IV ramp ahead — "
                    f"wings are at their cheapest now."
                ))
                log.info("%s pre-hedge reminder sent (%s)", _PREFIX, names)

            # prune old snapshots once per day (piggybacks the first tick)
            if not st.get("pruned"):
                st["pruned"] = True
                session.query(IvSnapshot).filter(
                    IvSnapshot.at < now - timedelta(days=_SNAPSHOT_RETENTION_DAYS)
                ).delete(synchronize_session=False)

            session.commit()
            _save_state(st)
    except Exception as exc:
        log.warning("%s tick failed: %s", _PREFIX, exc)


# ── /control card data ────────────────────────────────────────────────────────

def current_status(session: Any, settings: Any) -> list[dict]:
    """Latest tracked straddles for the /control defense card: one dict per
    straddle_key seen today, newest snapshot first."""
    from app.storage import IvSnapshot

    now = datetime.now(IST)
    st = _today_state(now)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(IvSnapshot)
        .filter(IvSnapshot.at >= day_start)
        .order_by(IvSnapshot.at.desc())
        .limit(600)
        .all()
    )
    latest: dict[str, Any] = {}
    series: dict[str, list[float]] = {}
    for r in rows:                       # newest → oldest
        if r.straddle_key not in latest:
            latest[r.straddle_key] = r
        if r.iv_pct is not None:
            series.setdefault(r.straddle_key, []).append(r.iv_pct)

    out = []
    for key, r in latest.items():
        ivs = series.get(key, [])[::-1]  # oldest → newest
        rec = st["straddles"].get(key, {})
        peak = rec.get("peak")
        out.append({
            "key": key,
            "underlying": r.underlying,
            "at": r.at,
            "mtm": r.mtm,
            "peak": peak,
            "drawdown": (peak - r.mtm) if (peak is not None and r.mtm is not None) else None,
            "iv_pct": r.iv_pct,
            "iv_rising": iv_rising(ivs, settings.STRADDLE_DEFENSE_IV_SAMPLES),
            "alerts": rec.get("alerts", 0),
        })
    out.sort(key=lambda d: d["underlying"])
    return out
