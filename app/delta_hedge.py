"""Delta-hedge dispatcher — ONE cron tick, many books.

This module is deliberately thin. The band engine lives in `app/hedge_core.py`
and each scrip owns its own spec module:

    app/ng_delta_hedge.py         NATURALGAS — spec + the NG-gated one-shots
    app/crudeoilm_delta_hedge.py  CRUDEOILM  — spec (band-based hedger)

**Never split this into one cron per underlying.** APScheduler's `max_instances`
only excludes a job from *itself*, so two jobs would each call `kite.positions()`,
race for the 1 req/sec quote bucket, and — the expensive failure — both place
margin-consuming orders in the same second. `_limit_then_market` blocks for up to
`NG_DELTA_HEDGE_LIMIT_WAIT_SEC`, so that overlap window is real. Splitting the
*code* per scrip (done) buys independent thresholds, toggles and state without
any of that; splitting the *tick* would not.

Each tick:
  1. Shared guards — emergency stop, session validity.
  2. Build the enabled specs. Each resolves its live thresholds from
     `app/hedge_tunables.py`, so a /control edit lands on this rebalance.
  3. Cadence gate (`_is_due`) — NG every 2 min at odd minutes, CRUDEOILM every
     5 min at :02. A spec crowded out last tick carries over and is forced.
  4. ONE `kite.positions()` call, shared by everything below.
  5. NG's one-shot helpers (half-exit / BNF stop-loss / straddle ladder).
  6. `hedge_core.process_underlying` per due spec, with **at most one placing
     orders per tick** — bounds tick duration and stops two books competing for
     the same margin.

Historically this file also held the engine, the one-shots and both specs. The
names below are re-exported so the F-move hedgers (GOLDM, SILVERM, CRUDEOILM)
and the test suite keep importing from `app.delta_hedge` unchanged — the
definitions moved, the import paths did not.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.hedge_core import (  # noqa: F401  (re-exported for back-compat)
    _ADX_CACHE,
    _ADX_CACHE_TTL_SEC,
    _ADX_MULT_CHOPPY,
    _ADX_MULT_MILD,
    _ADX_MULT_TREND,
    _ATM_CORRIDOR_PTS,
    _MIN_HEDGE_DELTA,
    _PRE_HEDGE_BOTH_PCT,
    _PRE_HEDGE_DELTA_PCT,
    _PRE_HEDGE_IMBALANCE_THRESH,
    HedgeSpec,
    _adx_regime_mult,
    _blank_state,
    _compute_band,
    _delta_trend,
    _delta_velocity_confirms,
    _get_futures_imbalance,
    _is_due,
    _is_insufficient_funds,
    _limit_then_market,
    _load_hedge_state,
    _lots_for,
    _matches_underlying,
    _notional_band_base,
    _pick_atm_strike,
    _pick_itm_strike,
    _process_underlying,
    _save_hedge_state,
    _spec_state,
    _telegram_notify,
    process_underlying,
)

log = logging.getLogger(__name__)

_LOG_PREFIX = "[hedge_dispatch]"


def _build_specs(settings: Any) -> list[HedgeSpec]:
    """Enabled hedge specs, most-volatile-first (NG leads CRUDEOILM).

    Built fresh every tick, and each scrip module resolves its own thresholds
    while constructing its spec — that is what makes a /control change apply
    from the next rebalance with no rebuild and no restart.
    """
    from app import crudeoilm_delta_hedge, ng_delta_hedge, state

    specs: list[HedgeSpec] = []
    if state.is_ng_hedge_enabled(settings.NG_DELTA_HEDGE_ENABLED):
        specs.append(ng_delta_hedge.build_spec(settings))
    if state.is_crude_hedge_enabled(settings.CRUDEOILM_HEDGE_ENABLED):
        specs.append(crudeoilm_delta_hedge.build_spec(settings))
    return specs


def run_delta_hedge_job(settings: Any, session_factory: Any) -> None:
    """One wake-up of the delta-hedge loop. Registered on the 1-min cron."""
    from app import ng_delta_hedge, state
    from app.config import IST
    from app.kite_session import get_session_manager

    if state.is_emergency_stop():
        log.info("%s emergency stop — skipping", _LOG_PREFIX)
        return
    if state.get_session_invalid():
        log.info("%s session invalid — skipping", _LOG_PREFIX)
        return

    specs = _build_specs(settings)
    if not specs:
        log.debug("%s no underlyings enabled — skipping", _LOG_PREFIX)
        return

    now = datetime.now(IST)
    all_state = _load_hedge_state(settings)
    due = [
        s for s in specs
        if _is_due(s, now, carryover=bool(_spec_state(all_state, s).get("carryover")))
    ]
    if not due:
        log.debug("%s nothing due at :%02d — specs=%s", _LOG_PREFIX, now.minute,
                  [f"{s.name}/{s.interval_min}m+{s.phase_min}" for s in specs])
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.warning("%s kite client unavailable: %s", _LOG_PREFIX, exc)
        return

    try:
        positions = kite.positions().get("net", []) or []
    except Exception as exc:
        log.warning("%s kite.positions() failed: %s", _LOG_PREFIX, exc)
        return

    # One-shots run once per tick, not once per due spec — they scan this same
    # positions payload and would otherwise fire N times. Still gated on the NG
    # toggle, which is what has governed them since they were written.
    if state.is_ng_hedge_enabled(settings.NG_DELTA_HEDGE_ENABLED):
        with session_factory() as session:
            if ng_delta_hedge.run_one_shots(settings, session, kite, positions):
                # Half-exit just restructured the book; any band computed from
                # the pre-exit payload is stale. Persist and wait for next tick.
                _save_hedge_state(settings, all_state)
                return

    hedged = False
    for spec in due:
        try:
            result = process_underlying(
                spec, settings, session_factory, kite, positions, now,
                all_state, allow_orders=not hedged,
            )
        except Exception:
            log.exception("%s tick failed", spec.prefix)
            continue
        if result == "HEDGED":
            hedged = True

    _save_hedge_state(settings, all_state)
