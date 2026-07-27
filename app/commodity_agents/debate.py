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
from dataclasses import dataclass
from typing import Any

from app.commodity_agents.llm_client import (
    ROLE_EVENT,
    ROLE_JUDGE,
    ROLE_TREND,
    ROLE_VOL,
    LlmClient,
    LlmError,
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
    "CRUDEOILM": (
        "High IV, gap-prone around OPEC+ meetings, weekly EIA crude inventory "
        "(Wednesdays), and geopolitical supply shocks. Better telegraphed than "
        "NG (OPEC calendar is public). Mini contract — good liquidity, cleaner "
        "IV readings than the thinly-traded full-size CRUDEOIL contract."
    ),
    "GOLDM": (
        "Lowest IV of the four; trends persist longer and technicals are "
        "cleaner. Drivers: US real yields, USD strength (DXY), Fed policy, "
        "safe-haven flows. Calmest short-premium candidate in quiet regimes. "
        "Mini contract — better liquidity than full-size GOLD."
    ),
    "SILVERM": (
        "Correlated to gold but higher beta, plus industrial demand (solar, "
        "electronics, China PMI). Thinner OTM liquidity than gold — wider "
        "bid-ask on adjustments; slippage matters. Mini contract — better "
        "liquidity than full-size SILVER."
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

# Every setup used to come back "medium" or "high" — across the whole recorded
# history not one agent ever returned "low", so the Judge never saw a clean
# setup and NO_TRADE became the only reachable answer. The flag needs a rubric,
# not just an enum.
_RISK_RUBRIC = """risk_flag rubric — calibrate honestly; most setups are NOT high:
  "low"    = nothing unusual pending, ordinary two-sided noise. A short-premium
             position would need an outsized surprise to be hurt.
  "medium" = a real risk you would size around (a scheduled print inside the
             horizon, a mild trend, thin wings) but not a reason to stand aside.
  "high"   = RESERVED. A specific, identifiable, near-term threat that makes
             selling premium a bad bet at ANY size. "This market can gap" is not
             high — every market can gap. If you cannot name the mechanism and
             say why it is not already in the price, it is medium.
Do not use "high" to express general caution; that is what bear_case is for.
Grading everything high makes the debate uninformative."""

_DEBATE_SCHEMA = """Return ONLY a valid JSON object (no markdown fences, no prose
outside JSON). Keep every string under 220 characters — terse desk shorthand:
{
  "stance": "<one-line position>",
  "bull_case": "<strongest argument for selling premium here>",
  "bear_case": "<strongest argument against>",
  "risk_flag": "low" | "medium" | "high",
  "confidence": <float 0.0-1.0>,
  "falsifier": "<specific observable that would invalidate your stance>"
}"""

# Analytics keys each role actually reasons about. Splitting these is most of
# the token saving: the strike chain and the full analytics blob used to be sent
# to all four roles even though only the Judge picks strikes.
_VOL_KEYS = ("vrp", "iv_trend", "expected_move", "term_structure", "stress")
_EVENT_KEYS = ("skew", "positioning", "india_vix")


def _round(obj: Any) -> Any:
    """Recursively round floats. Raw values like 0.24563175887667 spend tokens
    on precision no agent reasons about."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


def _slice(analytics: dict | None, keys: tuple[str, ...]) -> dict:
    if not analytics:
        return {}
    return {k: analytics[k] for k in keys if analytics.get(k) is not None}


def _compact_strikes(strikes: list[StrikeCandidate]) -> list[dict]:
    """Only the fields the Judge needs to choose a structure."""
    out = []
    for s in strikes:
        d = s.to_dict()
        out.append({
            "sym": d.get("tradingsymbol"),
            "type": d.get("instrument_type"),
            "strike": d.get("strike"),
            "ltp": d.get("ltp"),
            "iv": round(d["iv"], 3) if d.get("iv") is not None else None,
            "delta": round(d["delta"], 3) if d.get("delta") is not None else None,
        })
    return out


@dataclass(frozen=True)
class DebateContext:
    """Built once per run; each role renders only the slice it needs."""

    header: str
    regime: str
    price: str
    strikes: str
    events: str
    vol: str
    positioning: str

    @staticmethod
    def _join(*parts: str) -> str:
        return "\n".join(p for p in parts if p)

    def for_trend(self) -> str:
        return self._join(self.header, self.regime, self.price)

    def for_event(self) -> str:
        return self._join(self.header, self.regime, self.price, self.events,
                          self.positioning)

    def for_vol(self) -> str:
        return self._join(self.header, self.regime, self.price, self.vol)

    def for_judge(self) -> str:
        return self._join(self.header, self.regime, self.price, self.strikes,
                          self.events, self.vol, self.positioning)


def build_context(
    commodity: str,
    regime: RegimeSnapshot,
    candles_summary: dict,
    strikes: list[StrikeCandidate],
    events: list[EventWindow],
    analytics: dict | None = None,
) -> DebateContext:
    expiry = next((s.expiry for s in strikes), None)
    vol_slice = _slice(analytics, _VOL_KEYS)
    pos_slice = _slice(analytics, _EVENT_KEYS)
    return DebateContext(
        header=(f"COMMODITY: {commodity}\n"
                f"PROFILE: {COMMODITY_PROFILES.get(commodity, 'n/a')}"),
        regime=f"REGIME (deterministic classifier): {json.dumps(_round(regime.to_dict()))}",
        price=f"RECENT PRICE ACTION: {json.dumps(_round(candles_summary))}",
        strikes=(f"STRIKE CANDIDATES (Black-76"
                 f"{', expiry ' + str(expiry) if expiry else ''}): "
                 f"{json.dumps(_compact_strikes(strikes))}"),
        events=("UPCOMING SCHEDULED EVENTS: " + json.dumps([
            {"name": e.name, "time_ist": e.event_time.isoformat(), "impact": e.impact}
            for e in events
        ])),
        vol=(f"VOL ANALYTICS (VRP, IV trend, expected move vs breakevens, term "
             f"structure): {json.dumps(_round(vol_slice))}" if vol_slice else ""),
        positioning=(f"SKEW / POSITIONING: {json.dumps(_round(pos_slice))}"
                     if pos_slice else ""),
    )


# Role briefs live as constants because they are used twice: once per role in
# the four-call API path, and once concatenated in the single-call shim path
# (see run_full_debate). Editing a brief must change both, or the two backends
# would quietly grade the same setup differently.
_TREND_BRIEF = (
    "You are the Trend/Breakout debate agent on a commodity options desk "
    "run by a 20+ year options seller. Assess directional breakout risk to "
    "a short-premium position: could this market trend hard enough to hurt "
    "it? Weigh ADX, band width and price action from the context. Argue "
    "both sides honestly — a quiet, rangebound tape is a real and common "
    "finding, and saying so plainly is as useful as flagging danger. Be "
    "specific and falsifiable."
)

_EVENT_BRIEF = (
    "You are the Event/Gap-Risk debate agent on a commodity options desk. "
    "Assess asymmetric gap risk to a SHORT-PREMIUM position from (a) the "
    "scheduled events listed in the context and (b) breaking news you can "
    "find via web search — geopolitical supply shocks, weather anomalies, "
    "OPEC surprises. Scheduled events are ALREADY blackout-enforced by a "
    "deterministic layer upstream, so do not re-veto them; your job is the "
    "UNSCHEDULED risk, and whether the scheduled ones are bigger than the "
    "market has priced. Yours is the only risk_flag that hard-blocks short "
    "premium downstream, so spend 'high' carefully."
)

_VOL_BRIEF = (
    "You are the Volatility debate agent on a commodity options desk. "
    "Argue whether current implied vol is rich enough to compensate for "
    "tail risk (favors selling premium) or whether the market is "
    "underpricing risk (dangerous to sell). Use the ATM IV, realized vol, "
    "IV/RV ratio and IV percentile from the context, and the VOL ANALYTICS "
    "block when present: the VRP (IV minus realized), whether IV is "
    "expanding or contracting across recent runs, and the expected-move "
    "edge ratio (implied straddle move vs realized pace — above 1 favours "
    "the seller). An expanding IV trend argues for waiting even when VRP "
    "is positive; a fat, stable VRP is a genuine seller's edge and should "
    "be called as one."
)


def run_trend_agent(llm: LlmClient, ctx: DebateContext) -> dict:
    system = _TREND_BRIEF + "\n\n" + _RISK_RUBRIC + "\n\n" + _DEBATE_SCHEMA
    return llm.run(ROLE_TREND, system, ctx.for_trend())


def run_event_agent(llm: LlmClient, ctx: DebateContext) -> dict:
    system = _EVENT_BRIEF + "\n\n" + _RISK_RUBRIC + "\n\n" + _DEBATE_SCHEMA
    return llm.run(ROLE_EVENT, system, ctx.for_event())


def run_vol_agent(llm: LlmClient, ctx: DebateContext) -> dict:
    system = _VOL_BRIEF + "\n\n" + _RISK_RUBRIC + "\n\n" + _DEBATE_SCHEMA
    return llm.run(ROLE_VOL, system, ctx.for_vol())


_JUDGE_SCHEMA = """Return ONLY a valid JSON object:
{
  "headline": "<ONE plain-English sentence under 110 characters that a
                non-specialist could act on. NO jargon or abbreviations — no
                VRP, IV, ADX, skew, vega, theta, delta. Example: 'Options look
                expensive relative to how much this has actually been moving —
                worth selling, but keep it small.'>",
  "plain_reasons": ["<2-4 bullets, each under 120 characters, plain English, no
                      jargon. Translate the numbers instead of quoting them:
                      say 'the market is pricing bigger swings than it has
                      actually been making' rather than 'VRP +14 pts'.>"],
  "direction": "BUY" | "SELL" | "NO_TRADE",
  "strategy_type": "short_straddle" | "short_strangle" | "iron_fly" | "long_call" | "long_put" | "none",
  "strikes": [<tradingsymbols chosen from the provided candidates>],
  "confidence": <float 0.0-1.0>,
  "reasoning_summary": "<3-5 sentences of desk-level detail; jargon allowed here — this is the audit trail>",
  "dissenting_views": ["<every minority/risk view from the debate, verbatim gist — never drop these>"]
}"""


_JUDGE_RULES = (
    "You are the Judge on a commodity options desk. Synthesize the three "
    "debate outputs into ONE recommendation for an experienced short-"
    "premium options trader. Rules:\n"
    "1. ONLY the Event/Gap-Risk agent's risk_flag can block short premium, "
    "and only when it is exactly 'high'. This is enforced in code "
    "downstream. A 'high' from the Trend or Vol agent is EVIDENCE TO "
    "WEIGH, NOT A VETO — do not treat it as one, and do not cite this rule "
    "when standing aside on their flags.\n"
    "2. Preserve every dissenting or minority risk view in "
    "dissenting_views — never silently discard a risk flag.\n"
    "3. strikes must come from the provided candidates only.\n"
    "4. NO_TRADE is a legitimate answer but it is NOT the safe default. "
    "Choose it when the setup is genuinely unattractive: thin or negative "
    "VRP, a named near-term threat the market has not priced, or limits "
    "that plainly cannot be met. Do NOT choose it merely because the "
    "agents disagree — disagreement is the normal output of an adversarial "
    "debate. A rich VRP with 'medium' flags on both sides is a TRADE, "
    "sized down, not a pass.\n"
    "5. Prefer the defined-risk iron_fly (sell both 0.50-delta strikes, "
    "buy both 0.25-delta wings — list all four tradingsymbols in strikes) "
    "over a naked short_straddle when a high-impact event falls inside "
    "the horizon, the IV trend is expanding, or tail risk is elevated; "
    "the naked straddle is only for clean, quiet setups.\n"
    "6. headline and plain_reasons are read by someone who does not speak "
    "options. Keep them free of jargon even when reasoning_summary is "
    "dense — they must still convey the actual call, including when it is "
    "NO_TRADE and why."
)


def run_judge(
    llm: LlmClient,
    ctx: DebateContext,
    trend: dict,
    event: dict,
    vol: dict,
) -> dict:
    system = _JUDGE_RULES + "\n\n" + _JUDGE_SCHEMA
    user = (
        ctx.for_judge()
        + "\n\nTREND AGENT: " + json.dumps(trend)
        + "\nEVENT/GAP-RISK AGENT: " + json.dumps(event)
        + "\nVOL AGENT: " + json.dumps(vol)
    )
    return llm.run(ROLE_JUDGE, system, user)


_FULL_DEBATE_SCHEMA = f"""Return ONLY a valid JSON object with exactly four keys:
{{
  "trend": <the Trend agent's object>,
  "event": <the Event/Gap-Risk agent's object>,
  "vol":   <the Vol agent's object>,
  "judge": <the Judge's object>
}}

Each of trend, event and vol uses this shape:
{_DEBATE_SCHEMA}

judge uses this shape:
{_JUDGE_SCHEMA}"""


def run_full_debate(llm: LlmClient, ctx: DebateContext) -> dict:
    """All four roles in ONE model turn, returning {trend, event, vol, judge}.

    The four-call path (run_trend_agent → … → run_judge) is the right shape for
    the metered API, where per-call overhead is a few hundred tokens. It is the
    wrong shape for a CLI backend: a headless `claude -p` invocation carries
    ~18.9K tokens of fixed scaffolding, so four calls burn ~75K tokens of pure
    overhead. Collapsing to one turn is what makes the shim backend viable.

    The trade-off is real and worth naming: one turn means the three agents no
    longer reason independently, so their outputs correlate more than they do
    over the API. The role briefs are therefore reproduced verbatim (they are
    the same constants), and the judge is told explicitly to write each agent's
    view before ruling on it.
    """
    system = (
        "You are running a four-seat commodity options desk meeting by "
        "yourself. Play each seat in order, in full and in character, and do "
        "not let a later seat's conclusion leak backwards into an earlier "
        "one's analysis. Write each agent's verdict as that agent would — "
        "including when it undercuts the call you expect to end up making — "
        "and only then judge.\n\n"
        "=== SEAT 1 — TREND/BREAKOUT ===\n" + _TREND_BRIEF + "\n\n"
        "=== SEAT 2 — EVENT/GAP-RISK ===\n" + _EVENT_BRIEF + "\n\n"
        "=== SEAT 3 — VOLATILITY ===\n" + _VOL_BRIEF + "\n\n"
        "=== SEAT 4 — JUDGE ===\n" + _JUDGE_RULES + "\n\n"
        "The three agents share the risk-flag rubric below; grade "
        "independently against it, not relative to each other.\n\n"
        + _RISK_RUBRIC + "\n\n" + _FULL_DEBATE_SCHEMA
    )
    # for_judge() is the superset slice — it already contains everything the
    # three role slices carry, so one turn needs exactly one context render.
    return llm.run(ROLE_JUDGE, system, ctx.for_judge())


def split_full_debate(out: dict) -> tuple[dict, dict, dict, dict]:
    """Unpack run_full_debate's reply into (trend, event, vol, judge).

    Validated rather than .get()-ed: a missing judge would otherwise surface as
    an empty recommendation that looks like a real NO_TRADE.
    """
    missing = [k for k in ("trend", "event", "vol", "judge")
               if not isinstance(out.get(k), dict)]
    if missing:
        raise LlmError(f"full-debate reply missing sections: {missing} "
                       f"(got keys {sorted(out)})")
    return out["trend"], out["event"], out["vol"], out["judge"]


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
        vetoed["headline"] = (
            "Standing aside — there is a specific near-term event that could "
            "move this sharply against the position."
        )
        vetoed["plain_reasons"] = [
            "The event-risk check flagged a serious upcoming risk, which "
            "automatically blocks selling options here.",
            "This block is applied by a fixed rule, not by judgement, so it "
            "cannot be argued around.",
        ]
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
