"""LLM debate round (trend / event-gap-risk / vol) + Judge synthesis.

Every agent must return structured JSON with an explicit confidence and a
falsifier ("what would change my mind") — falsifiability is what makes the
reasoning trail auditable after the fact.

Two rules are enforced in CODE, not just in the Judge's prompt:
  * if the event agent flags gap_risk == "high", any short-premium
    recommendation is downgraded to NO_TRADE;
  * dissenting views are always carried into the final output.
"""
from __future__ import annotations

import json
from typing import Any

from app.commodity_agents.llm_client import (
    ROLE_EVENT,
    ROLE_JUDGE,
    ROLE_TREND,
    ROLE_VOL,
    LlmClient,
)
from app.commodity_agents.models import EventWindow, RegimeSnapshot, StrikeCandidate

# Per-commodity trading character (Section 2 of the design brief) — injected
# into every prompt so the agents reason about THIS commodity, not a generic one.
COMMODITY_PROFILES = {
    "NATURALGAS": (
        "Extremely high IV, prone to violent overnight gaps (especially winter). "
        "Primary drivers: weather (heating/cooling demand) and the weekly EIA "
        "storage report (Thursdays). Highest short-premium risk of the four — "
        "storage surprises can move NG 10%+ intraday. Thin liquidity far OTM."
    ),
    "CRUDEOIL": (
        "High IV, gap-prone around OPEC+ meetings, weekly EIA crude inventory "
        "(Wednesdays), and geopolitical supply shocks. Better telegraphed than "
        "NG (OPEC calendar is public). Good option liquidity."
    ),
    "GOLD": (
        "Lowest IV of the four; trends persist longer and technicals are "
        "cleaner. Drivers: US real yields, USD strength (DXY), Fed policy, "
        "safe-haven flows. Calmest short-premium candidate in quiet regimes."
    ),
    "SILVER": (
        "Correlated to gold but higher beta, plus industrial demand (solar, "
        "electronics, China PMI). Thinner OTM liquidity than gold — wider "
        "bid-ask on adjustments; slippage matters."
    ),
    "NIFTY": (
        "NSE index options (NFO), the deepest option market in India — "
        "excellent liquidity, tight spreads, weekly expiries. Drivers: FII "
        "flows, global risk sentiment, RBI policy, US market overnight moves "
        "(gap risk at 09:15 open). IV usually modest; short premium works in "
        "calm regimes but expiry-day gamma is violent — check days-to-expiry "
        "on the candidates before recommending short premium."
    ),
    "BANKNIFTY": (
        "NSE bank-index options (NFO), monthly expiries only. Higher beta and "
        "IV than NIFTY; concentrated in a handful of large banks so single-"
        "name news (results, RBI actions on a bank) can gap it. Very liquid "
        "near ATM. Rate-sensitive: RBI MPC days are the key scheduled risk."
    ),
}

_DEBATE_SCHEMA = """Return ONLY a valid JSON object (no markdown fences, no prose outside JSON):
{
  "stance": "<one-line position>",
  "bull_case": "<strongest argument one way>",
  "bear_case": "<strongest argument the other way>",
  "risk_flag": "low" | "medium" | "high",
  "confidence": <float 0.0-1.0>,
  "falsifier": "<specific observable that would invalidate your stance>"
}"""


def _fmt_context(
    commodity: str,
    regime: RegimeSnapshot,
    candles_summary: dict,
    strikes: list[StrikeCandidate],
    events: list[EventWindow],
    analytics: dict | None = None,
) -> str:
    ctx = (
        f"COMMODITY: MCX {commodity}\n"
        f"PROFILE: {COMMODITY_PROFILES.get(commodity, 'n/a')}\n"
        f"REGIME (deterministic classifier): {json.dumps(regime.to_dict())}\n"
        f"RECENT PRICE ACTION: {json.dumps(candles_summary)}\n"
        f"STRIKE CANDIDATES (Black-76): "
        f"{json.dumps([s.to_dict() for s in strikes])}\n"
        f"UPCOMING SCHEDULED EVENTS: "
        + json.dumps([
            {"name": e.name, "time_ist": e.event_time.isoformat(), "impact": e.impact}
            for e in events
        ])
    )
    if analytics:
        ctx += ("\nQUANT ANALYTICS (deterministic — VRP, IV trend, expected move "
                "vs breakevens, stress scenarios): " + json.dumps(analytics))
    return ctx


def run_trend_agent(llm: LlmClient, ctx: str) -> dict:
    system = (
        "You are the Trend/Breakout debate agent on a commodity options desk "
        "run by a 20+ year options seller. Argue BOTH sides of directional "
        "breakout risk for the given setup: could this market trend hard "
        "enough to hurt a short-premium position? Weigh ADX, band width, and "
        "price action from the context. Be specific and falsifiable.\n\n"
        + _DEBATE_SCHEMA
    )
    return llm.run(ROLE_TREND, system, ctx)


def run_event_agent(llm: LlmClient, ctx: str) -> dict:
    system = (
        "You are the Event/Gap-Risk debate agent on a commodity options desk. "
        "Assess asymmetric gap risk to a SHORT-PREMIUM position from (a) the "
        "scheduled events listed in the context and (b) breaking news you can "
        "find via web search — geopolitical supply shocks, weather anomalies, "
        "OPEC surprises. Scheduled events are already blackout-enforced by a "
        "deterministic layer; your job is the UNSCHEDULED risk and the "
        "magnitude of the scheduled ones. risk_flag=high means: do not sell "
        "premium here.\n\n" + _DEBATE_SCHEMA
    )
    return llm.run(ROLE_EVENT, system, ctx)


def run_vol_agent(llm: LlmClient, ctx: str) -> dict:
    system = (
        "You are the Volatility debate agent on a commodity options desk. "
        "Argue whether current implied vol is rich enough to compensate for "
        "tail risk (favors selling premium) or whether the market is "
        "underpricing risk (dangerous to sell). Use the ATM IV, realized vol, "
        "IV/RV ratio and IV percentile from the context, and the QUANT "
        "ANALYTICS block when present: the VRP (IV minus realized), whether IV "
        "is expanding or contracting across recent runs, and the expected-move "
        "edge ratio (implied straddle move vs realized pace — above 1 favours "
        "the seller). An expanding IV trend argues for waiting even when VRP "
        "is positive.\n\n" + _DEBATE_SCHEMA
    )
    return llm.run(ROLE_VOL, system, ctx)


_JUDGE_SCHEMA = """Return ONLY a valid JSON object:
{
  "direction": "BUY" | "SELL" | "NO_TRADE",
  "strategy_type": "short_straddle" | "short_strangle" | "iron_fly" | "long_call" | "long_put" | "none",
  "strikes": [<tradingsymbols chosen from the provided candidates>],
  "confidence": <float 0.0-1.0>,
  "reasoning_summary": "<3-6 sentences a human can audit later>",
  "dissenting_views": ["<every minority/risk view from the debate, verbatim gist — never drop these>"]
}"""


def run_judge(
    llm: LlmClient,
    ctx: str,
    trend: dict,
    event: dict,
    vol: dict,
) -> dict:
    system = (
        "You are the Judge on a commodity options desk. Synthesize the three "
        "debate outputs into ONE recommendation for an experienced short-"
        "premium options trader. Rules:\n"
        "1. You may only greenlight a short-premium strategy if the Event/"
        "Gap-Risk agent's risk_flag is NOT 'high' (this is also enforced in "
        "code — do not fight it).\n"
        "2. Preserve every dissenting or minority risk view in "
        "dissenting_views — never silently discard a risk flag.\n"
        "3. strikes must come from the provided candidates only.\n"
        "4. NO_TRADE is a first-class answer; prefer it when the debate is "
        "genuinely split.\n"
        "5. Prefer the defined-risk iron_fly (sell both 0.50-delta strikes, "
        "buy both 0.25-delta wings — list all four tradingsymbols in strikes) "
        "over a naked short_straddle when a high-impact event falls inside "
        "the horizon, the IV trend is expanding, or tail risk is elevated; "
        "the naked straddle is only for clean, quiet setups.\n\n" + _JUDGE_SCHEMA
    )
    user = (
        ctx
        + "\n\nTREND AGENT: " + json.dumps(trend)
        + "\nEVENT/GAP-RISK AGENT: " + json.dumps(event)
        + "\nVOL AGENT: " + json.dumps(vol)
    )
    return llm.run(ROLE_JUDGE, system, user)


SHORT_PREMIUM_TYPES = {"short_straddle", "short_strangle", "short_call", "short_put", "iron_fly"}


def enforce_gap_risk_gate(judge_out: dict, event_out: dict) -> dict:
    """Code-level enforcement: high gap risk kills short premium, always."""
    if (
        event_out.get("risk_flag") == "high"
        and judge_out.get("strategy_type") in SHORT_PREMIUM_TYPES
    ):
        vetoed = dict(judge_out)
        vetoed["direction"] = "NO_TRADE"
        vetoed["strategy_type"] = "none"
        vetoed["reasoning_summary"] = (
            "OVERRIDDEN IN CODE: event/gap-risk agent flagged HIGH gap risk; "
            "short-premium recommendation blocked. Original judge reasoning: "
            + str(judge_out.get("reasoning_summary", ""))
        )
        dissent = list(judge_out.get("dissenting_views") or [])
        dissent.append("Judge originally recommended "
                       f"{judge_out.get('strategy_type')} despite high gap risk flag")
        vetoed["dissenting_views"] = dissent
        return vetoed
    return judge_out


def summarize_candles(candles: list[dict], n: int = 20) -> dict:
    """Compact price-action summary for prompts — keeps token cost sane."""
    tail = candles[-n:]
    closes = [c["close"] for c in tail]
    return {
        "candles_used": len(tail),
        "first_close": closes[0] if closes else None,
        "last_close": closes[-1] if closes else None,
        "high": max((c["high"] for c in tail), default=None),
        "low": min((c["low"] for c in tail), default=None),
        "change_pct": (
            round(100.0 * (closes[-1] / closes[0] - 1), 2)
            if len(closes) >= 2 and closes[0] else None
        ),
    }
