"""Live portfolio Greeks + delta-drift alerts.

A short straddle goes on delta-neutral; the market then pushes it. A pro
watches the position's net delta and adjusts when one side gets tested —
he does not wait for the stop. This module computes per-position and
per-straddle Greeks from live quotes and pushes a Telegram alert when a
straddle's net per-lot delta drifts past the configured threshold.

Read-only: it never places or modifies orders. Suggestions go to the human.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any

from app.commodity_agents.models import INDEX_UNDERLYINGS
from app.config import IST
from app.greeks import compute_delta

log = logging.getLogger(__name__)

_SECONDS_PER_YEAR = 31_557_600
_EXPIRY_HHMM = {"MCX": (17, 0), "NFO": (15, 30)}
_ALERT_COOLDOWN_S = 3600            # one drift alert per straddle per hour
_last_alert: dict[str, float] = {}


def _vega_theta(exch: str, flag: str, under: float, strike: float,
                t: float, r: float, sigma: float) -> tuple[float | None, float | None]:
    try:
        if exch == "NFO":
            from py_vollib.black_scholes_merton.greeks.analytical import theta, vega
            return vega(flag, under, strike, t, r, sigma, 0.0), \
                   theta(flag, under, strike, t, r, sigma, 0.0)
        from py_vollib.black.greeks.analytical import theta, vega
        return vega(flag, under, strike, t, r, sigma), \
               theta(flag, under, strike, t, r, sigma)
    except Exception:
        return None, None


def compute_portfolio_greeks(session: Any, kite: Any, settings: Any,
                             now: datetime | None = None) -> dict:
    """Greeks for every open option position on MCX/NFO. Never raises —
    positions whose inputs are missing are reported with nulls."""
    from app.commodity_agents.orchestrator import _underlying_price
    from app.storage import Instrument, Order, Position

    now = now or datetime.now(IST)
    rows = (
        session.query(Position, Order)
        .join(Order, Position.order_id == Order.id)
        .filter(Position.exchange.in_(["MCX", "MCX-OPT", "NFO"]),
                Position.instrument_type.in_(["CE", "PE"]))
        .all()
    )
    if not rows:
        return {"positions": [], "straddles": {}, "totals": {}}

    # one quote round for all option legs
    keys = [f"{pos.exchange}:{pos.tradingsymbol}" for pos, _ in rows]
    try:
        quotes = kite.quote(keys)
    except Exception as exc:
        log.warning("[portfolio] quote fetch failed: %s", exc)
        quotes = {}

    under_cache: dict[str, float | None] = {}
    out_rows: list[dict] = []
    straddles: dict[str, dict] = {}
    tot_delta_units = 0.0
    tot_vega = 0.0
    tot_theta = 0.0

    for pos, order in rows:
        if pos.underlying not in under_cache:
            try:
                under_cache[pos.underlying] = _underlying_price(kite, session, pos.underlying, now)
            except Exception:
                under_cache[pos.underlying] = None
        under = under_cache[pos.underlying]

        inst = (
            session.query(Instrument)
            .filter(Instrument.tradingsymbol == pos.tradingsymbol,
                    Instrument.exchange == pos.exchange)
            .first()
        )
        q = quotes.get(f"{pos.exchange}:{pos.tradingsymbol}") or {}
        ltp = float(q.get("last_price") or 0.0)

        entry: dict = {
            "tradingsymbol": pos.tradingsymbol,
            "underlying": pos.underlying,
            "side": order.transaction_type,
            "quantity": pos.quantity,
            "ltp": ltp or None,
            "delta": None, "vega": None, "theta_per_day": None, "iv": None,
        }
        if inst is not None and under and ltp > 0 and inst.expiry:
            exch = "NFO" if pos.exchange == "NFO" else "MCX"
            hh, mm = _EXPIRY_HHMM[exch]
            expiry_dt = datetime(inst.expiry.year, inst.expiry.month, inst.expiry.day,
                                 hh, mm, tzinfo=IST)
            t_years = max((expiry_dt - now).total_seconds() / _SECONDS_PER_YEAR, 1.0 / 8760)
            g = compute_delta(inst, ltp, under, t_years)
            if g.delta is not None:
                sign = -1.0 if order.transaction_type == "SELL" else 1.0
                mcx_units = (settings.MCX_LOT_UNITS.get(pos.underlying, 1)
                             if exch == "MCX" else 1)
                eff_units = pos.quantity * mcx_units
                flag = "c" if pos.instrument_type == "CE" else "p"
                vega, theta = _vega_theta(exch, flag, under, float(inst.strike or 0.0),
                                          t_years, settings.RISK_FREE_RATE, g.iv or 0.0)
                entry.update({
                    "delta": round(sign * g.delta, 3),          # per unit, signed
                    "iv": round((g.iv or 0.0) * 100.0, 2),
                    "vega": round(sign * vega * eff_units, 1) if vega is not None else None,
                    "theta_per_day": round(sign * theta * eff_units, 1) if theta is not None else None,
                })
                tot_delta_units += sign * g.delta * eff_units
                if vega is not None:
                    tot_vega += sign * vega * eff_units
                if theta is not None:
                    tot_theta += sign * theta * eff_units

                sid = order.straddle_id
                if sid:
                    grp = straddles.setdefault(sid, {
                        "underlying": pos.underlying, "legs": [],
                        "net_delta_per_lot": 0.0,
                    })
                    grp["legs"].append(pos.tradingsymbol)
                    grp["net_delta_per_lot"] = round(
                        grp["net_delta_per_lot"] + sign * g.delta, 3)
        out_rows.append(entry)

    return {
        "positions": out_rows,
        "straddles": straddles,
        "totals": {
            "net_delta_units": round(tot_delta_units, 1),
            "net_vega": round(tot_vega, 1),
            "net_theta_per_day": round(tot_theta, 1),
        },
    }


def check_delta_drift(session_factory: Any, settings: Any) -> None:
    """Scheduler job: alert when a straddle's net per-lot delta drifts past
    COMMODITY_DELTA_ALERT_THRESHOLD. Read-only; throttled per straddle."""
    from app.kite_session import get_session_manager
    try:
        kite = get_session_manager().get_kite()
    except Exception:
        return
    try:
        with session_factory() as session:
            book = compute_portfolio_greeks(session, kite, settings)
    except Exception as exc:
        log.error("[portfolio] greeks computation failed: %s", exc)
        return

    threshold = settings.COMMODITY_DELTA_ALERT_THRESHOLD
    now_mono = time.monotonic()
    for sid, grp in book["straddles"].items():
        net = grp["net_delta_per_lot"]
        if abs(net) < threshold:
            continue
        if now_mono - _last_alert.get(sid, 0.0) < _ALERT_COOLDOWN_S:
            continue
        _last_alert[sid] = now_mono
        tested = "CALL" if net > 0 else "PUT"
        other = "put" if net > 0 else "call"
        from app.commodity_agents.notify import send_telegram
        send_telegram(settings, (
            f"⚠️ <b>{grp['underlying']}</b> straddle delta drift: net "
            f"{net:+.2f}/lot (threshold ±{threshold:.2f})\n"
            f"{tested} side is being tested. Consider rolling the {other} leg "
            f"toward ATM to re-centre, or exiting — do not wait for the stop.\n"
            f"Legs: {', '.join(grp['legs'])}"
        ))
        log.info("[portfolio] delta drift alert %s net=%+.2f", grp["underlying"], net)
