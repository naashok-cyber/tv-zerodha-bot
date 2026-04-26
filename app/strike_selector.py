from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.config import IST, Settings, get_settings
from app.greeks import GreeksResult, compute_delta
from app.storage import Instrument, StrikeDecision

log = logging.getLogger(__name__)

_QUOTE_CHUNK = 500          # Kite API limit per quote() call
_SECONDS_PER_YEAR = 365.25 * 24 * 3600


class NoValidStrikeError(Exception):
    """Raised when no strike survives delta + liquidity guardrails."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class StrikeSelection:
    instrument: Instrument
    computed_delta: float
    iv: float
    option_ltp: float
    candidates_considered: int              # total instruments found (before filtering)
    rejection_reasons: dict[str, str]       # tradingsymbol → reason for any rejected candidate


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def _quote_all(kite_client, keys: list[str]) -> dict:
    """Fetch quotes in ≤500-instrument chunks and merge."""
    result: dict = {}
    for i in range(0, len(keys), _QUOTE_CHUNK):
        result.update(kite_client.quote(keys[i:i + _QUOTE_CHUNK]))
    return result


def _is_index(underlying: str, weekly_set: set[str]) -> bool:
    return underlying.upper() in weekly_set


def _guardrail_premium(ltp: float, is_index: bool, s: Settings) -> str | None:
    threshold = s.MIN_OPTION_PREMIUM_INDEX if is_index else s.MIN_OPTION_PREMIUM_STOCK
    if ltp < threshold:
        return f"ltp {ltp} below min_premium {threshold}"
    return None


def _guardrail_oi(oi: float, is_index: bool, s: Settings) -> str | None:
    threshold = s.MIN_OI_INDEX if is_index else s.MIN_OI_STOCK
    if oi < threshold:
        return f"oi {oi} below min_oi {threshold}"
    return None


def _guardrail_spread(bid: float, ask: float, ltp: float, s: Settings) -> str | None:
    spread_pct = (ask - bid) / ltp if ltp > 0 else float("inf")
    if spread_pct > s.MAX_SPREAD_PCT:
        return f"spread {spread_pct:.3%} > max {s.MAX_SPREAD_PCT:.3%}"
    return None


# ── Main selector ─────────────────────────────────────────────────────────────

def select_strike(
    underlying: str,
    expiry: date,
    flag: str,                          # "CE" or "PE"
    kite_client,                        # KiteConnect instance
    spot_or_future_ltp: float,
    session: Session,
    alert_id: int | None = None,
    segment: str = "NFO",
    target_delta: float | None = None,
    tolerance: float | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> StrikeSelection:
    """Select the best-delta option strike from a live option chain.

    flag:               "CE" for calls, "PE" for puts
    spot_or_future_ltp: underlying spot (NSE/BFO) or futures LTP (MCX)
    session:            used to query instruments and persist the audit row

    Raises NoValidStrikeError when no candidate survives all filters.
    """
    s = settings or get_settings()
    now_ist = now or datetime.now(IST)
    target = target_delta if target_delta is not None else s.TARGET_DELTA
    tol = tolerance if tolerance is not None else s.DELTA_TOLERANCE
    weekly_set = {u.upper() for u in s.WEEKLY_INDICES}
    index_flag = _is_index(underlying, weekly_set)

    # 1. Fetch all option instruments for this chain.
    instruments: list[Instrument] = (
        session.query(Instrument)
        .filter(
            Instrument.name == underlying,
            Instrument.expiry == expiry,
            Instrument.instrument_type == flag,
            Instrument.strike.isnot(None),
        )
        .order_by(Instrument.strike)
        .all()
    )

    if not instruments:
        _persist(session, alert_id, underlying, expiry, flag, target, [], None, None)
        raise NoValidStrikeError(f"No {flag} instruments found for {underlying} expiry {expiry}")

    # 2. Compute session-close time for time_to_expiry.
    is_mcx = "MCX" in segment.upper()
    close_hm = _parse_hhmm(s.SESSION_CLOSE_MCX if is_mcx else s.SESSION_CLOSE_NSE)
    expiry_close = datetime(
        expiry.year, expiry.month, expiry.day, close_hm[0], close_hm[1], tzinfo=IST
    )
    t = max(0.0, (expiry_close - now_ist).total_seconds()) / _SECONDS_PER_YEAR

    # 3. Batch-fetch LTPs.
    quote_keys = [f"{inst.exchange}:{inst.tradingsymbol}" for inst in instruments]
    quotes = _quote_all(kite_client, quote_keys)

    # 4–5. For each instrument: compute delta, apply guardrails.
    candidates_json: list[dict] = []
    rejected: dict[str, str] = {}

    @dataclass
    class _Candidate:
        instrument: Instrument
        delta: float
        iv: float
        ltp: float

    valid: list[_Candidate] = []

    for inst in instruments:
        key = f"{inst.exchange}:{inst.tradingsymbol}"
        quote = quotes.get(key, {})
        ltp = float(quote.get("last_price", 0.0))
        oi = float(quote.get("oi", 0))
        depth = quote.get("depth", {})
        buy_d = depth.get("buy", [])
        sell_d = depth.get("sell", [])

        entry: dict = {
            "strike": inst.strike,
            "tradingsymbol": inst.tradingsymbol,
            "ltp": ltp,
            "oi": oi,
            "delta": None,
            "iv": None,
            "rejection_reason": None,
        }

        # Zero-price guard (also catches missing quote).
        if ltp <= 0:
            reason = "zero_price"
            rejected[inst.tradingsymbol] = reason
            entry["rejection_reason"] = reason
            candidates_json.append(entry)
            continue

        # Greeks.
        gr: GreeksResult = compute_delta(inst, ltp, spot_or_future_ltp, t)
        if gr.rejection_reason:
            rejected[inst.tradingsymbol] = gr.rejection_reason
            entry["rejection_reason"] = gr.rejection_reason
            candidates_json.append(entry)
            continue

        entry["delta"] = gr.delta
        entry["iv"] = gr.iv

        # Premium guardrail.
        if (reason := _guardrail_premium(ltp, index_flag, s)) is not None:
            rejected[inst.tradingsymbol] = reason
            entry["rejection_reason"] = reason
            candidates_json.append(entry)
            continue

        # OI guardrail.
        if (reason := _guardrail_oi(oi, index_flag, s)) is not None:
            rejected[inst.tradingsymbol] = reason
            entry["rejection_reason"] = reason
            candidates_json.append(entry)
            continue

        # Spread guardrail — only when depth is available.
        if buy_d and sell_d:
            bid, ask = float(buy_d[0]["price"]), float(sell_d[0]["price"])
            if (reason := _guardrail_spread(bid, ask, ltp, s)) is not None:
                rejected[inst.tradingsymbol] = reason
                entry["rejection_reason"] = reason
                candidates_json.append(entry)
                continue
        else:
            log.warning("No depth data for %s; skipping spread check", inst.tradingsymbol)

        valid.append(_Candidate(inst, float(gr.delta), float(gr.iv), ltp))  # type: ignore[arg-type]
        candidates_json.append(entry)

    if not valid:
        _persist(session, alert_id, underlying, expiry, flag, target, candidates_json, None, None)
        raise NoValidStrikeError(
            f"All {len(instruments)} candidate(s) for {underlying} {flag} rejected by guardrails"
        )

    # 6. Find candidate whose |delta| is closest to target.
    valid.sort(key=lambda c: abs(abs(c.delta) - target))

    # 7. Tolerance check on best.
    best = valid[0]
    if abs(abs(best.delta) - target) > tol:
        _persist(session, alert_id, underlying, expiry, flag, target, candidates_json, None, None)
        raise NoValidStrikeError(
            f"Best delta {best.delta:.4f} for {underlying} {flag} exceeds tolerance "
            f"(|Δ - {target}| = {abs(abs(best.delta) - target):.4f} > {tol})"
        )

    # 8. Tie-breaker within 0.001: prefer ITM (higher |delta|) side.
    tie_candidates = [
        c for c in valid
        if abs(abs(abs(c.delta) - target) - abs(abs(best.delta) - target)) <= 0.001
    ]
    if len(tie_candidates) > 1:
        # ITM = higher abs(delta)
        best = max(tie_candidates, key=lambda c: abs(c.delta))

    # 9. Persist audit row.
    _persist(session, alert_id, underlying, expiry, flag, target, candidates_json, best, None)

    return StrikeSelection(
        instrument=best.instrument,
        computed_delta=best.delta,
        iv=best.iv,
        option_ltp=best.ltp,
        candidates_considered=len(instruments),
        rejection_reasons=rejected,
    )


def _persist(
    session: Session,
    alert_id: int | None,
    underlying: str,
    expiry: date,
    flag: str,
    target_delta: float,
    candidates_json: list[dict],
    best,           # _Candidate or None
    error_reason: str | None,
) -> None:
    row = StrikeDecision(
        alert_id=alert_id,
        underlying=underlying,
        expiry=str(expiry),
        flag=flag[0].lower(),           # "CE" → "c", "PE" → "p"
        target_delta=target_delta,
        candidates_json=json.dumps(candidates_json),
        selected_tradingsymbol=best.instrument.tradingsymbol if best else None,
        selected_strike=best.instrument.strike if best else None,
        selected_delta=best.delta if best else None,
        selected_iv=best.iv if best else None,
        selected_ltp=best.ltp if best else None,
        rejection_reason=error_reason,
        decided_at=datetime.now(IST),
    )
    session.add(row)
    session.commit()
