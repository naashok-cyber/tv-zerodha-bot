"""BANKNIFTY delta-hedge cron job — NSE-native, 1-lot fixed, SELL-first.

Runs every 1 min during NSE hours (09:15–15:30 IST). On each tick:
  1. Pull open BANKNIFTY NFO option + futures legs
  2. Compute net portfolio delta (BSM) using **spot BANKNIFTY** as F
     (NSE options use spot, not futures, unlike the MCX delta hedge)
  3. If |net δ| > BNF_HEDGE_BAND_UNITS, pick deficit side (δ>0 → CE, δ<0 → PE)
  4. **SELL 1 lot** nearest-ATM at LIMIT@bid → escalate to MARKET
  5. If SELL is blocked by insufficient margin → **BUY 1 lot** opposite-type
     nearest-ATM (bounded loss = premium paid, no margin lock)
  6. Update matching Position.quantity so end-of-day squareoff closes full
     broker qty; warn if no Position row exists (fresh strike)
  7. Telegram-notify the action

Deliberately kept in its own module rather than plugged into
`run_delta_hedge_job`: NSE has different position-quantity units (Kite reports
NFO qty in units, MCX in lots), different session close, and a different F
source (spot vs. front-month futures). Merging would clutter the shared code
with `if exchange == "MCX"` branches.

Fixed 1-lot hedge means the band must be > 1-lot ATM delta or the book
pingpongs: for BNF (lot_size=30, δ_ATM≈0.50) that's 15 units. Default band
25 leaves ~10 units of residual after hedge = 40% of band, mirroring the
NG hedge's NG_HEDGE_RESIDUAL_RATIO design.
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

_LOG_PREFIX = "[bnf_hedge]"
_UNDERLYING = "BANKNIFTY"
_SPOT_KEY = "NSE:NIFTY BANK"
_EXCHANGE = "NFO"


def _matches_bnf(tradingsymbol: str) -> bool:
    """Prefix must be BANKNIFTY and next char a digit (expiry). Prevents
    matching any hypothetical BANKNIFTY-adjacent symbol."""
    if not tradingsymbol.startswith(_UNDERLYING):
        return False
    rest = tradingsymbol[len(_UNDERLYING):]
    return bool(rest) and rest[0].isdigit()


def _load_state(settings: Any) -> dict:
    """Per-day hedge state — resets on date rollover. Tracks hedges_today,
    last_hedge_at, and any error/telemetry we want to persist across ticks."""
    from app.config import IST as _IST
    today = datetime.now(_IST).date().isoformat()
    path = getattr(settings, "BNF_HEDGE_STATE_PATH", "")
    blank = {"date": today, "hedges_today": 0, "last_hedge_at": None}
    if not path or not os.path.exists(path):
        return blank
    try:
        with open(path) as f:
            state = json.load(f)
    except Exception as exc:
        log.warning("%s state file unreadable (%s) — starting fresh", _LOG_PREFIX, exc)
        return blank
    if state.get("date") != today:
        log.info("%s state from %s — resetting for %s",
                 _LOG_PREFIX, state.get("date"), today)
        return blank
    return {**blank, **state}


def _save_state(settings: Any, state: dict) -> None:
    path = getattr(settings, "BNF_HEDGE_STATE_PATH", "")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        log.error("%s failed to save state %s: %s", _LOG_PREFIX, path, exc)


def _pick_atm_strike(session: Any, expiry: Any, opt_type: str, F: float) -> Any:
    """Nearest strike to F on the given side (CE/PE) for BNF, same expiry."""
    from app.storage import Instrument
    candidates = (
        session.query(Instrument)
        .filter(
            Instrument.name == _UNDERLYING,
            Instrument.instrument_type == opt_type,
            Instrument.exchange == _EXCHANGE,
            Instrument.expiry == expiry,
        )
        .all()
    )
    if not candidates:
        return None
    return min(candidates, key=lambda inst: abs(float(inst.strike or 0) - F))


def _telegram(settings: Any, text: str) -> None:
    """Best-effort HTML Telegram push."""
    try:
        from app.commodity_agents.notify import send_telegram
        send_telegram(settings, text)
    except Exception as exc:
        log.warning("%s telegram send failed: %s", _LOG_PREFIX, exc)


def run_bnf_hedge_job(settings: Any, session_factory: Any) -> None:
    """One wake-up of the BNF hedge loop. Called by APScheduler every minute.

    All exceptions are swallowed with a log — this must never crash the
    scheduler thread. Read `state.is_bnf_hedge_enabled` each tick so the
    feature can be toggled without a restart (mirrors partial_booking /
    straddle_defense patterns)."""
    try:
        _run_impl(settings, session_factory)
    except Exception:
        log.exception("%s tick failed", _LOG_PREFIX)


def _run_impl(settings: Any, session_factory: Any) -> None:
    from app import hedge_tunables, state
    from app.config import IST
    from app.greeks import compute_delta
    from app.kite_session import get_session_manager
    from app.orders import throttled_quote_call
    from app.storage import ClosedTrade, Instrument, Order, Position

    if not getattr(settings, "BNF_HEDGE_ENABLED", False):
        return
    if state.is_emergency_stop():
        log.debug("%s emergency stop — skipping", _LOG_PREFIX)
        return
    if state.get_session_invalid():
        log.debug("%s session invalid — skipping", _LOG_PREFIX)
        return

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.warning("%s kite unavailable: %s", _LOG_PREFIX, exc)
        return

    try:
        positions = kite.positions().get("net", []) or []
    except Exception as exc:
        log.warning("%s kite.positions() failed: %s", _LOG_PREFIX, exc)
        return

    now = datetime.now(IST)
    # Thresholds resolved once per tick, /control override first and .env behind
    # it. Resolved in one pass so a single hedge decision can never be computed
    # from two generations of the config (an edit landing mid-tick).
    tun = hedge_tunables.all_effective(settings, "banknifty")

    bnf_opts = [
        p for p in positions
        if p.get("exchange") == _EXCHANGE
        and p.get("quantity", 0) != 0
        and _matches_bnf(p.get("tradingsymbol") or "")
        and (p["tradingsymbol"].endswith("CE") or p["tradingsymbol"].endswith("PE"))
    ]
    bnf_futs = [
        p for p in positions
        if p.get("quantity", 0) != 0
        and _matches_bnf(p.get("tradingsymbol") or "")
        and p["tradingsymbol"].endswith("FUT")
    ]

    if not bnf_opts and not bnf_futs:
        log.debug("%s no open BNF legs — nothing to hedge", _LOG_PREFIX)
        return
    if not bnf_opts:
        log.debug("%s only futures — no delta drift to chase on 1-δ book", _LOG_PREFIX)
        return

    # Spot for F. Key must be the exact string Kite exposes for the index.
    try:
        F = float(throttled_quote_call(kite.ltp, [_SPOT_KEY])[_SPOT_KEY]["last_price"])
    except Exception as exc:
        log.warning("%s spot fetch failed: %s", _LOG_PREFIX, exc)
        return
    if F <= 0:
        log.warning("%s spot invalid (%s) — skipping", _LOG_PREFIX, F)
        return

    with session_factory() as session:
        # Options expiry from any leg's Instrument row
        sample = session.query(Instrument).filter_by(
            tradingsymbol=bnf_opts[0]["tradingsymbol"]
        ).first()
        if sample is None or sample.expiry is None:
            log.warning("%s no instrument row for %s", _LOG_PREFIX, bnf_opts[0]["tradingsymbol"])
            return
        opt_expiry = sample.expiry

        # T using NSE close, NOT MCX close
        close_h, close_m = (int(x) for x in settings.SESSION_CLOSE_NSE.split(":"))
        expiry_dt = datetime.combine(opt_expiry, dtime(close_h, close_m), tzinfo=IST)
        T = (expiry_dt - now).total_seconds() / 31_557_600.0
        min_days = float(getattr(settings, "BNF_HEDGE_MIN_T_DAYS", 1.0))
        if T <= (min_days / 365.0):
            log.info(
                "%s T=%.4fy (<%.2fd min) — skipping near-expiry (gamma bomb)",
                _LOG_PREFIX, T, min_days,
            )
            return

        # Batch LTP fetch for all option legs
        leg_fulls = [f"{_EXCHANGE}:{p['tradingsymbol']}" for p in bnf_opts]
        try:
            ltp_resp = throttled_quote_call(kite.ltp, leg_fulls)
        except Exception as exc:
            log.warning("%s ltp batch failed: %s", _LOG_PREFIX, exc)
            return

        total_delta = 0.0
        # Futures legs: NFO FUT quantity is in units, δ=1 per unit
        for fp in bnf_futs:
            fd = int(fp["quantity"]) * 1.0
            total_delta += fd
            log.info("%s FUT leg %s qty=%+d δ=%+.1f",
                     _LOG_PREFIX, fp["tradingsymbol"], fp["quantity"], fd)

        per_leg: list[tuple[str, Any, int, float]] = []  # (sym, inst, qty, δ_unit)
        for p in bnf_opts:
            sym = p["tradingsymbol"]
            inst = session.query(Instrument).filter_by(tradingsymbol=sym).first()
            if inst is None:
                log.warning("%s no instrument row for %s — skip leg", _LOG_PREFIX, sym)
                continue
            ltp = ltp_resp.get(f"{_EXCHANGE}:{sym}", {}).get("last_price") or 0
            if ltp <= 0:
                log.warning("%s LTP missing for %s — skip leg", _LOG_PREFIX, sym)
                continue
            res = compute_delta(inst, float(ltp), F, T)
            if res.delta is None:
                # Deep-ITM near expiry: BS IV solver often fails. Fall back to
                # sign-only signal: CE ITM ⇒ δ≈+1, PE ITM ⇒ δ≈−1 to at least
                # keep the leg accounted for in the net (better than dropping
                # it entirely, which would silently under-report the book δ).
                strike = float(inst.strike or 0)
                if inst.instrument_type == "CE" and F > strike:
                    approx = 0.95
                elif inst.instrument_type == "PE" and F < strike:
                    approx = -0.95
                else:
                    log.warning("%s δ failed for %s and not deep-ITM — skip leg",
                                _LOG_PREFIX, sym)
                    continue
                log.warning("%s δ solver failed for %s (deep ITM) — using approx %+.2f",
                            _LOG_PREFIX, sym, approx)
                res_delta = approx
            else:
                res_delta = res.delta
            qty = int(p["quantity"])  # NFO qty is already in units
            leg_delta = qty * res_delta
            total_delta += leg_delta
            per_leg.append((sym, inst, qty, res_delta))
            log.info(
                "%s %s %s qty=%+d @ %.2f δ_unit=%+.4f → δ=%+.1f",
                _LOG_PREFIX, inst.instrument_type, sym, qty, ltp, res_delta, leg_delta,
            )

        if not per_leg and not bnf_futs:
            log.warning("%s no leg deltas computed — skipping", _LOG_PREFIX)
            return

        band = float(tun["band_units"])
        n_lots_open = sum(abs(int(p["quantity"])) // 30 for p in bnf_opts)
        log.info(
            "%s F=%.2f T=%.3fy(%.1fd) net δ=%+.1f | band=±%.0f | open lots=%d",
            _LOG_PREFIX, F, T, T*365, total_delta, band, n_lots_open,
        )

        # ── Circuit breakers ────────────────────────────────────────────────
        # Freeze near NSE close — a hedge placed at 15:20 gets marked-to-close
        # 10 min later, paying spread for no benefit.
        freeze_min = int(tun["freeze_before_min"])
        freeze_bypass = float(getattr(settings, "BNF_HEDGE_FREEZE_BYPASS_MULT", 3.0))
        if freeze_min > 0:
            freeze_from = (
                datetime.combine(now.date(), dtime(close_h, close_m), tzinfo=IST)
                - timedelta(minutes=freeze_min)
            )
            if now >= freeze_from and abs(total_delta) <= (freeze_bypass * band):
                log.info(
                    "%s [FREEZE] within %d min of %s close — holding (δ=%+.0f)",
                    _LOG_PREFIX, freeze_min, settings.SESSION_CLOSE_NSE, total_delta,
                )
                return

        hedge_state = _load_state(settings)
        # Daily hedge cap — spend guardrail against a runaway trend day
        daily_cap = int(tun["max_hedges_per_day"])
        if daily_cap > 0 and int(hedge_state.get("hedges_today", 0)) >= daily_cap:
            log.info(
                "%s daily cap reached (%d hedges today) — holding",
                _LOG_PREFIX, daily_cap,
            )
            return

        # Cooldown — one hedge, then let the move develop
        cooldown_min = int(tun["cooldown_min"])
        cooldown_bypass = float(getattr(settings, "BNF_HEDGE_COOLDOWN_BYPASS_MULT", 2.0))
        last_hedge = hedge_state.get("last_hedge_at")
        if cooldown_min > 0 and last_hedge:
            try:
                since = (now - datetime.fromisoformat(last_hedge)).total_seconds() / 60.0
            except (ValueError, TypeError):
                since = None
            if (since is not None and since < cooldown_min
                    and abs(total_delta) <= cooldown_bypass * band):
                log.info(
                    "%s [COOLDOWN] last hedge %.1f min ago (<%d) — holding (δ=%+.0f)",
                    _LOG_PREFIX, since, cooldown_min, total_delta,
                )
                return

        if abs(total_delta) <= band:
            return

        # ── Decide the hedge ────────────────────────────────────────────────
        # δ > 0 (long-biased) → need negative δ → SELL CE (adds −δ_unit×qty)
        # δ < 0 (short-biased) → need positive δ → SELL PE (PE δ<0, sell flips sign)
        primary_type = "CE" if total_delta > 0 else "PE"
        primary_inst = _pick_atm_strike(session, opt_expiry, primary_type, F)
        if primary_inst is None:
            log.warning("%s no ATM %s strike for %s — skipping",
                        _LOG_PREFIX, primary_type, opt_expiry)
            return

        # Estimate expected δ change from the SELL (log-only; sizing is fixed 1 lot)
        primary_ltp = 0.0
        try:
            primary_key = f"{_EXCHANGE}:{primary_inst.tradingsymbol}"
            primary_ltp = float(throttled_quote_call(kite.ltp, [primary_key])[primary_key]["last_price"])
        except Exception as exc:
            log.warning("%s primary LTP fetch failed: %s", _LOG_PREFIX, exc)

        primary_delta_unit: float | None = None
        if primary_ltp > 0:
            res = compute_delta(primary_inst, primary_ltp, F, T)
            primary_delta_unit = res.delta

        qty = int(primary_inst.lot_size)  # 30 units = 1 lot
        # Signed δ change of a SELL: −qty × δ_unit
        est_added = -qty * (primary_delta_unit or 0.5 * (1 if primary_type == "CE" else -1))
        est_projected = total_delta + est_added
        log.info(
            "%s HEDGE PLAN: SELL 1 lot (%d units) %s @ ~%.2f "
            "(δ_unit≈%+.4f est.added=%+.0f → proj δ=%+.0f)",
            _LOG_PREFIX, qty, primary_inst.tradingsymbol, primary_ltp,
            primary_delta_unit if primary_delta_unit is not None else 0.0,
            est_added, est_projected,
        )

        if settings.DRY_RUN:
            log.info("%s DRY_RUN — order not placed", _LOG_PREFIX)
            _telegram(
                settings,
                f"<b>[BNF hedge · DRY]</b> would SELL 1 lot {primary_inst.tradingsymbol}\n"
                f"net δ before: {total_delta:+.0f}\n"
                f"est δ after:  {est_projected:+.0f}\n"
                f"band: ±{band:.0f}",
            )
            return

        oid, mode, action_desc, final_inst, final_side, final_qty, final_added = _try_hedge(
            kite, session, settings, primary_inst, "SELL", qty,
            total_delta, opt_expiry, F, T,
        )

        if oid is None:
            log.error(
                "%s all hedge attempts failed (mode=%s, net δ=%+.0f) — no hedge applied",
                _LOG_PREFIX, mode, total_delta,
            )
            _telegram(
                settings,
                f"<b>[BNF hedge · FAILED]</b>\n"
                f"net δ: {total_delta:+.0f} (band ±{band:.0f})\n"
                f"Tried SELL {primary_inst.tradingsymbol} → BUY opposite. "
                f"All rejected. Manual attention required.",
            )
            return

        # Bookkeeping: update matching Position if it exists so EOD squareoff
        # closes the full broker qty. If not, warn — user gets telegram anyway.
        projected_delta = total_delta + final_added
        pos = (
            session.query(Position)
            .join(Order, Position.order_id == Order.id)
            .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
            .filter(
                Position.tradingsymbol == final_inst.tradingsymbol,
                Position.exchange == _EXCHANGE,
                ClosedTrade.id == None,  # noqa: E711
                Order.dry_run == False,  # noqa: E712
            )
            .first()
        )
        if pos is not None:
            old_qty = pos.quantity
            signed_qty = final_qty if final_side == "BUY" else -final_qty
            pos.quantity = old_qty + signed_qty
            pos.last_updated_at = now
            session.commit()
            log.info(
                "%s Position.id=%d qty %d→%d (hedge %s %d)",
                _LOG_PREFIX, pos.id, old_qty, pos.quantity, final_side, final_qty,
            )
        else:
            log.warning(
                "%s no open Position row for %s — squareoff may leave %d %s units uncovered",
                _LOG_PREFIX, final_inst.tradingsymbol, final_qty, final_side,
            )

        # Persist state
        hedge_state["hedges_today"] = int(hedge_state.get("hedges_today", 0)) + 1
        hedge_state["last_hedge_at"] = now.isoformat()
        _save_state(settings, hedge_state)

        _telegram(
            settings,
            f"<b>[BNF hedge]</b> {action_desc} ({mode})\n"
            f"net δ before: {total_delta:+.0f}\n"
            f"net δ after (est): {projected_delta:+.0f}\n"
            f"band: ±{band:.0f} | hedge #{hedge_state['hedges_today']} today\n"
            f"order_id: {oid}",
        )


def _is_insufficient_funds(exc: BaseException) -> bool:
    return "Insufficient funds" in str(exc) or "insufficient margin" in str(exc).lower()


def _try_hedge(
    kite: Any, session: Any, settings: Any,
    primary_inst: Any, primary_side: str, qty: int,
    total_delta: float, opt_expiry: Any, F: float, T: float,
) -> tuple[str | None, str, str, Any, str, int, float]:
    """Attempt the primary SELL; on INSUFFICIENT_FUNDS, fall back to BUY opposite.

    Returns (oid, mode, action_desc, final_inst, final_side, final_qty, final_added).
    Only the LTP/δ needed to compute `final_added` is re-fetched — the primary's
    numbers are recomputed at fill time to keep the log line honest.
    """
    from app.delta_hedge import _limit_then_market
    from app.greeks import compute_delta

    # Primary SELL
    oid, mode = _limit_then_market(kite, primary_inst, primary_side, qty, settings)
    if oid is not None:
        # Best-effort read of the achieved δ for the log — LTP is a fair proxy
        try:
            key = f"{_EXCHANGE}:{primary_inst.tradingsymbol}"
            ltp = float(kite.ltp([key])[key]["last_price"])
            du = compute_delta(primary_inst, ltp, F, T).delta or 0.0
        except Exception:
            du = 0.0
        added = -qty * du if primary_side == "SELL" else qty * du
        return (oid, mode, f"SELL 1 lot {primary_inst.tradingsymbol}",
                primary_inst, primary_side, qty, added)

    # Fallback: BUY the opposite-type ATM (bounded loss = premium; no margin lock).
    # Positive δ over → need negative → BUY PE
    # Negative δ over → need positive → BUY CE
    if mode == "INSUFFICIENT_FUNDS":
        opp_type = "PE" if total_delta > 0 else "CE"
        opp_inst = _pick_atm_strike(session, opt_expiry, opp_type, F)
        if opp_inst is None:
            log.warning("%s fallback: no ATM %s strike", _LOG_PREFIX, opp_type)
            return (None, "FAILED", "", primary_inst, primary_side, qty, 0.0)

        # Re-solve size just in case: for BNF this is still 1 lot = 30 units
        opp_qty = int(opp_inst.lot_size)
        try:
            key = f"{_EXCHANGE}:{opp_inst.tradingsymbol}"
            opp_ltp = float(kite.ltp([key])[key]["last_price"])
        except Exception:
            opp_ltp = 0.0
        opp_du = compute_delta(opp_inst, opp_ltp, F, T).delta if opp_ltp > 0 else None
        if opp_du is None:
            # PE δ is negative; CE δ is positive. Approximate ATM at ±0.5.
            opp_du = -0.5 if opp_type == "PE" else 0.5

        log.info(
            "%s margin-blocked SELL → fallback: BUY 1 lot %s (δ_unit≈%+.4f)",
            _LOG_PREFIX, opp_inst.tradingsymbol, opp_du,
        )
        oid2, mode2 = _limit_then_market(kite, opp_inst, "BUY", opp_qty, settings)
        if oid2 is not None:
            added = opp_qty * opp_du
            return (oid2, mode2, f"BUY 1 lot {opp_inst.tradingsymbol}",
                    opp_inst, "BUY", opp_qty, added)

    return (None, mode, "", primary_inst, primary_side, qty, 0.0)
