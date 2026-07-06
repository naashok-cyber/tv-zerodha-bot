"""Deterministic Risk Guard — hard veto power, cannot be overridden.

Pure function of its inputs: no LLM, no I/O. If any check fails the
recommendation is downgraded to NO-TRADE regardless of how confident the
debate/judge output was. The Judge never sees or negotiates with this layer.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.commodity_agents.models import EventWindow, RiskVerdict

SHORT_PREMIUM_STRATEGIES = {"short_straddle", "short_strangle", "short_call", "short_put",
                            "iron_fly"}


@dataclass(frozen=True)
class RiskLimits:
    max_loss_per_lot: float          # ₹ worst-case per lot the trader accepts
    max_daily_loss: float            # ₹ realized-loss circuit breaker
    max_concurrent_commodities: int  # open commodity positions cap
    max_margin_util_pct: float       # margin_required/margin_available cap
    max_lots: int


@dataclass(frozen=True)
class RiskInput:
    commodity: str
    strategy_type: str               # judge's strategy_type; "none" passes trivially
    lots: int
    est_max_loss_per_lot: float | None   # None = could not estimate → veto short premium
    daily_realized_pnl: float        # negative when losing
    open_commodity_count: int
    margin_available: float | None
    margin_required: float | None
    blackout: EventWindow | None     # active high-impact blackout window, if any
    # open underlyings in the same correlation group already carrying short
    # option positions (energy / metals / indices) — defaults keep old callers valid
    open_group_shorts: int = 0
    group_name: str | None = None


def evaluate(limits: RiskLimits, inp: RiskInput) -> RiskVerdict:
    reasons: list[str] = []

    if inp.strategy_type in ("none", "", None):
        return RiskVerdict(approved=True, reasons=[])

    is_short_premium = inp.strategy_type in SHORT_PREMIUM_STRATEGIES

    if is_short_premium and inp.blackout is not None:
        reasons.append(
            f"blackout window active: {inp.blackout.name} at "
            f"{inp.blackout.event_time.strftime('%Y-%m-%d %H:%M IST')}"
        )

    if -inp.daily_realized_pnl >= limits.max_daily_loss:
        reasons.append(
            f"daily loss circuit breaker: realized {inp.daily_realized_pnl:.0f} "
            f"vs cap -{limits.max_daily_loss:.0f}"
        )

    if inp.lots > limits.max_lots:
        reasons.append(f"lots {inp.lots} > cap {limits.max_lots}")

    if is_short_premium:
        if inp.est_max_loss_per_lot is None:
            reasons.append("cannot estimate max loss per lot for short premium")
        elif inp.est_max_loss_per_lot > limits.max_loss_per_lot:
            reasons.append(
                f"est. max loss/lot {inp.est_max_loss_per_lot:.0f} > "
                f"cap {limits.max_loss_per_lot:.0f}"
            )

    if is_short_premium and inp.open_group_shorts >= 1:
        reasons.append(
            f"correlated short-vega exposure: {inp.open_group_shorts} underlying(s) "
            f"in the {inp.group_name or 'same'} group already carry short option "
            f"positions — one macro shock would hit both"
        )

    if inp.open_commodity_count >= limits.max_concurrent_commodities:
        reasons.append(
            f"open commodity positions {inp.open_commodity_count} >= "
            f"cap {limits.max_concurrent_commodities}"
        )

    if inp.margin_available is not None and inp.margin_required is not None:
        if inp.margin_available <= 0:
            reasons.append("no margin available")
        else:
            util = 100.0 * inp.margin_required / inp.margin_available
            if util > limits.max_margin_util_pct:
                reasons.append(
                    f"margin utilization {util:.0f}% > cap {limits.max_margin_util_pct:.0f}%"
                )

    return RiskVerdict(approved=not reasons, reasons=reasons)
