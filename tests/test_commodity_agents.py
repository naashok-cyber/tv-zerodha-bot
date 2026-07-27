"""Tests for the commodity debate-agent pipeline: events, regime, risk guard,
strikes, gap-risk gate, and the end-to-end orchestrator with a fake LLM."""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.commodity_agents import debate, events, regime, risk_guard, strikes
from app.commodity_agents.models import (
    HIGH_VOL_EXPANSION,
    PRE_EVENT_COMPRESSION,
    RANGE_BOUND,
    TRENDING,
    EventWindow,
    StrikeCandidate,
)
from app.commodity_agents.storage import AgentRun, CommodityRecommendation
from app.config import IST
from app.storage import Base, Instrument


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _mk_candles(closes: list[float], spread: float = 0.5) -> list[dict]:
    return [{"high": c + spread, "low": c - spread, "close": c} for c in closes]


# ── event calendar ───────────────────────────────────────────────────────────

class TestEvents:
    def test_ng_eia_thursday(self):
        # Wed 2026-07-08 12:00 IST → EIA NG storage Thu 2026-07-09 20:00 within horizon
        now = datetime(2026, 7, 8, 12, 0, tzinfo=IST)
        evs = events.upcoming_events("NATURALGAS", now, horizon_hours=48)
        names = [e.name for e in evs]
        assert "EIA Natural Gas Storage Report" in names
        eia = next(e for e in evs if "Storage" in e.name)
        assert eia.event_time.weekday() == 3
        assert eia.event_time.hour == 20

    def test_eia_ng_irrelevant_to_gold(self):
        now = datetime(2026, 7, 8, 12, 0, tzinfo=IST)
        evs = events.upcoming_events("GOLDM", now, horizon_hours=96)
        assert not any("Natural Gas" in e.name for e in evs)

    def test_blackout_active_inside_window(self):
        # Thu 18:30 IST — 1.5h before the 20:00 EIA NG report, pre_hours=3
        now = datetime(2026, 7, 9, 18, 30, tzinfo=IST)
        bo = events.active_blackout("NATURALGAS", now, pre_hours=3, post_hours=1)
        assert bo is not None and "Storage" in bo.name

    def test_no_blackout_well_before(self):
        now = datetime(2026, 7, 9, 10, 0, tzinfo=IST)
        assert events.active_blackout("NATURALGAS", now, pre_hours=3, post_hours=1) is None

    def test_fomc_hits_gold_not_ng(self):
        now = datetime(2026, 7, 28, 12, 0, tzinfo=IST)   # day before Jul-29 FOMC
        gold = events.upcoming_events("GOLDM", now, horizon_hours=48)
        ng = events.upcoming_events("NATURALGAS", now, horizon_hours=48)
        assert any("FOMC" in e.name for e in gold)
        assert not any("FOMC" in e.name for e in ng)


# ── regime classifier ────────────────────────────────────────────────────────

class TestRegime:
    def _flat_closes(self, n=60, base=100.0, wiggle=0.15):
        rng = random.Random(42)
        return [base + rng.uniform(-wiggle, wiggle) for _ in range(n)]

    def test_range_bound(self):
        snap = regime.classify_regime(
            _mk_candles(self._flat_closes()), periods_per_year=252,
            atm_iv=0.3, iv_history=[], in_event_window=False,
        )
        assert snap.label == RANGE_BOUND

    def test_trending_on_steady_climb(self):
        closes = [100 + i * 1.5 for i in range(60)]
        snap = regime.classify_regime(
            _mk_candles(closes), periods_per_year=252,
            atm_iv=None, iv_history=[], in_event_window=False,
        )
        assert snap.label == TRENDING
        assert snap.adx is not None and snap.adx >= 25

    def test_high_vol_expansion_beats_trending(self):
        # calm then violent: last 5 closes swing hugely
        closes = self._flat_closes(55) + [100, 108, 94, 110, 92]
        snap = regime.classify_regime(
            _mk_candles(closes), periods_per_year=252,
            atm_iv=0.5, iv_history=[], in_event_window=True,
        )
        assert snap.label == HIGH_VOL_EXPANSION

    def test_pre_event_compression(self):
        snap = regime.classify_regime(
            _mk_candles(self._flat_closes()), periods_per_year=252,
            atm_iv=0.3, iv_history=[], in_event_window=True,
        )
        assert snap.label == PRE_EVENT_COMPRESSION

    def test_iv_percentile_needs_history(self):
        assert regime.iv_percentile(0.4, [0.3] * 5) is None
        assert regime.iv_percentile(0.4, [0.3] * 10) == 100.0
        assert regime.iv_percentile(0.2, [0.3] * 10) == 0.0

    # A moderate ramp is what the MCX evening session looks like every day.
    # Before IV confirmation this alone skipped 94% of production runs.
    def _session_ramp(self):
        """Calm baseline then a 2.0x RV pickup with one >3-sigma candle — the
        old ratio + sigma test fires on this, so only IV can settle it."""
        return self._flat_closes(55) + [100.50, 100.35, 100.42, 100.34, 100.45]

    def test_moderate_ramp_not_expansion_without_iv_history(self):
        snap = regime.classify_regime(
            _mk_candles(self._session_ramp()), periods_per_year=252,
            atm_iv=0.5, iv_history=[], in_event_window=False,
        )
        assert snap.label != HIGH_VOL_EXPANSION
        assert any("not confirmed by IV" in n for n in snap.notes)

    def test_moderate_ramp_not_expansion_when_iv_is_calm(self):
        # ATM IV sits at the bottom of its own range: options are not pricing
        # a regime change, so neither do we.
        snap = regime.classify_regime(
            _mk_candles(self._session_ramp()), periods_per_year=252,
            atm_iv=0.20, iv_history=[0.30 + i * 0.01 for i in range(20)],
            in_event_window=False,
        )
        assert snap.label != HIGH_VOL_EXPANSION
        assert snap.iv_percentile is not None and snap.iv_percentile < 80.0

    def test_moderate_ramp_is_expansion_when_iv_confirms(self):
        snap = regime.classify_regime(
            _mk_candles(self._session_ramp()), periods_per_year=252,
            atm_iv=0.90, iv_history=[0.30 + i * 0.01 for i in range(20)],
            in_event_window=False,
        )
        assert snap.label == HIGH_VOL_EXPANSION
        assert any("ATM IV at" in n for n in snap.notes)


# ── risk guard ───────────────────────────────────────────────────────────────

def _limits(**kw):
    base = dict(max_loss_per_lot=15000, max_daily_loss=10000,
                max_concurrent_commodities=2, max_margin_util_pct=60, max_lots=5)
    base.update(kw)
    return risk_guard.RiskLimits(**base)


def _inp(**kw):
    base = dict(commodity="GOLDM", strategy_type="short_straddle", lots=1,
                est_max_loss_per_lot=8000.0, daily_realized_pnl=0.0,
                open_commodity_count=0, margin_available=500000.0,
                margin_required=None, blackout=None)
    base.update(kw)
    return risk_guard.RiskInput(**base)


class TestRiskGuard:
    def test_clean_pass(self):
        v = risk_guard.evaluate(_limits(), _inp())
        assert v.approved and v.reasons == []

    def test_blackout_vetoes_short_premium(self):
        bo = EventWindow("EIA", "NATURALGAS",
                         datetime(2026, 7, 9, 20, 0, tzinfo=IST),
                         datetime(2026, 7, 9, 17, 0, tzinfo=IST),
                         datetime(2026, 7, 9, 21, 0, tzinfo=IST), "high")
        v = risk_guard.evaluate(_limits(), _inp(blackout=bo))
        assert not v.approved and any("blackout" in r for r in v.reasons)

    def test_blackout_does_not_block_long_options(self):
        bo = EventWindow("EIA", "NATURALGAS",
                         datetime(2026, 7, 9, 20, 0, tzinfo=IST),
                         datetime(2026, 7, 9, 17, 0, tzinfo=IST),
                         datetime(2026, 7, 9, 21, 0, tzinfo=IST), "high")
        v = risk_guard.evaluate(_limits(), _inp(strategy_type="long_call",
                                                est_max_loss_per_lot=None, blackout=bo))
        assert v.approved

    def test_daily_loss_circuit_breaker(self):
        v = risk_guard.evaluate(_limits(), _inp(daily_realized_pnl=-12000.0))
        assert not v.approved and any("circuit breaker" in r for r in v.reasons)

    def test_per_lot_loss_cap(self):
        v = risk_guard.evaluate(_limits(), _inp(est_max_loss_per_lot=20000.0))
        assert not v.approved

    def test_unknown_loss_vetoes_short_premium(self):
        v = risk_guard.evaluate(_limits(), _inp(est_max_loss_per_lot=None))
        assert not v.approved

    def test_concurrency_cap(self):
        v = risk_guard.evaluate(_limits(), _inp(open_commodity_count=2))
        assert not v.approved

    def test_none_strategy_passes(self):
        v = risk_guard.evaluate(_limits(), _inp(strategy_type="none",
                                                daily_realized_pnl=-99999.0))
        assert v.approved


# ── gap-risk gate (code-level judge override) ────────────────────────────────

class TestGapRiskGate:
    def test_high_gap_risk_kills_short_premium(self):
        judge = {"direction": "SELL", "strategy_type": "short_straddle",
                 "strikes": ["X"], "confidence": 0.9,
                 "reasoning_summary": "sell it", "dissenting_views": []}
        out = debate.enforce_gap_risk_gate(judge, {"risk_flag": "high"})
        assert out["direction"] == "NO_TRADE"
        assert out["strategy_type"] == "none"
        assert any("high gap risk" in d for d in out["dissenting_views"])

    def test_low_gap_risk_untouched(self):
        judge = {"direction": "SELL", "strategy_type": "short_straddle",
                 "strikes": ["X"], "confidence": 0.9,
                 "reasoning_summary": "sell it", "dissenting_views": []}
        assert debate.enforce_gap_risk_gate(judge, {"risk_flag": "low"}) is judge

    def test_high_gap_risk_leaves_long_options(self):
        judge = {"direction": "BUY", "strategy_type": "long_call",
                 "strikes": ["X"], "confidence": 0.8,
                 "reasoning_summary": "buy it", "dissenting_views": []}
        assert debate.enforce_gap_risk_gate(judge, {"risk_flag": "high"}) is judge

    def test_veto_rewrites_the_plain_summary(self):
        # The judge's own headline describes a trade that is no longer
        # happening — leaving it on screen would be actively misleading.
        judge = {"direction": "SELL", "strategy_type": "short_straddle",
                 "strikes": ["X"], "confidence": 0.9,
                 "reasoning_summary": "sell it", "dissenting_views": [],
                 "headline": "Selling premium here looks good.",
                 "plain_reasons": ["Options look expensive."]}
        out = debate.enforce_gap_risk_gate(judge, {"risk_flag": "high"})
        assert out["headline"] != judge["headline"]
        assert out["plain_reasons"] != judge["plain_reasons"]


class TestTrendGate:
    """A trending tape is how a sold straddle takes its worst loss, so the
    trend veto is enforced in code rather than left to the Judge's prompt."""

    @staticmethod
    def _snap(label, adx):
        from app.commodity_agents.models import RegimeSnapshot
        return RegimeSnapshot(label=label, adx=adx, bb_width_pct=None,
                              realized_vol=None, atm_iv=None, iv_rv_ratio=None,
                              iv_percentile=None)

    @staticmethod
    def _judge(strategy="short_straddle"):
        return {"direction": "SELL", "strategy_type": strategy,
                "strikes": ["X"], "confidence": 0.9,
                "reasoning_summary": "premium is rich", "dissenting_views": [],
                "headline": "Sell it.", "plain_reasons": ["Options look dear."]}

    def test_range_bound_is_left_alone(self):
        out = debate.enforce_trend_gate(self._judge(), self._snap(RANGE_BOUND, 14.0), 32.0)
        assert out["direction"] == "SELL"

    def test_trend_blocks_a_naked_straddle(self):
        out = debate.enforce_trend_gate(self._judge(), self._snap(TRENDING, 27.0), 32.0)
        assert out["direction"] == "NO_TRADE"
        assert out["strategy_type"] == "none"
        assert any("ADX 27.0" in d for d in out["dissenting_views"])
        # Rich premium is not a counter-argument; the headline must say so.
        assert "trending" in out["headline"].lower()

    def test_mild_trend_still_allows_capped_wings(self):
        out = debate.enforce_trend_gate(self._judge("iron_fly"),
                                        self._snap(TRENDING, 27.0), 32.0)
        assert out["strategy_type"] == "iron_fly"

    def test_very_strong_trend_blocks_even_an_iron_fly(self):
        out = debate.enforce_trend_gate(self._judge("iron_fly"),
                                        self._snap(TRENDING, 38.0), 32.0)
        assert out["direction"] == "NO_TRADE"

    def test_very_strong_adx_blocks_whatever_the_label_says(self):
        # The classifier ranks high-vol-expansion above trending, so a violent
        # directional blowout carries a different label with the same danger.
        out = debate.enforce_trend_gate(self._judge(),
                                        self._snap(HIGH_VOL_EXPANSION, 41.0), 32.0)
        assert out["direction"] == "NO_TRADE"

    def test_missing_adx_leaves_the_verdict_to_the_judge(self):
        # Too few candles to compute ADX. Inventing a trend reading either way
        # would be worse than deferring to the debate that already ran.
        judge = self._judge()
        assert debate.enforce_trend_gate(judge, self._snap(TRENDING, None), 32.0) is judge

    def test_no_trade_verdicts_pass_through_untouched(self):
        judge = self._judge("none")
        assert debate.enforce_trend_gate(judge, self._snap(TRENDING, 40.0), 32.0) is judge

    def test_judge_schema_offers_no_long_side(self):
        # The desk sells premium only — a directional view is a reason to
        # stand aside, never a trade of its own.
        assert "long_call" not in debate._JUDGE_SCHEMA
        assert "long_put" not in debate._JUDGE_SCHEMA
        assert '"BUY"' not in debate._JUDGE_SCHEMA
        for brief in (debate._JUDGE_RULES, debate.run_full_debate.__doc__ or ""):
            assert "long_call" not in brief


# ── context slicing (token budget) ───────────────────────────────────────────

class TestDebateContext:
    def _ctx(self):
        snap = regime.classify_regime(
            _mk_candles([100 + i * 0.1 for i in range(60)]), periods_per_year=252,
            atm_iv=0.3, iv_history=[], in_event_window=False,
        )
        strikes = [
            StrikeCandidate(
                tradingsymbol=f"GOLDM26JUL{72000 + i * 100}CE", strike=72000.0 + i * 100,
                instrument_type="CE", expiry="2026-07-28", ltp=250.0 + i,
                iv=0.245631758876675, delta=0.5031415926535, target_delta=0.50,
                lot_units=10,
            )
            for i in range(6)
        ]
        return debate.build_context(
            "GOLDM", snap, debate.summarize_candles(_mk_candles([100.0] * 30)),
            strikes, [],
            analytics={"vrp": 0.0512345678, "iv_trend": {"slope": 0.0011111111},
                       "skew": {"rr25": -0.0212345}, "positioning": {"pcr": 1.23456}},
        )

    def test_each_role_gets_only_its_slice(self):
        ctx = self._ctx()
        # The strike chain is the single biggest block and only the Judge
        # prices strikes, so it must not be paid for three extra times.
        assert "STRIKE CANDIDATES" not in ctx.for_trend()
        assert "STRIKE CANDIDATES" not in ctx.for_event()
        assert "STRIKE CANDIDATES" not in ctx.for_vol()
        assert "STRIKE CANDIDATES" in ctx.for_judge()
        assert "VOL ANALYTICS" in ctx.for_vol()
        assert "VOL ANALYTICS" not in ctx.for_trend()
        assert "SKEW / POSITIONING" in ctx.for_event()
        assert "SKEW / POSITIONING" not in ctx.for_vol()

    def test_every_slice_is_smaller_than_the_whole(self):
        ctx = self._ctx()
        full = len(ctx.for_judge())
        for part in (ctx.for_trend(), ctx.for_event(), ctx.for_vol()):
            assert len(part) < full

    def test_floats_are_rounded(self):
        ctx = self._ctx()
        assert "0.245631758876675" not in ctx.for_judge()
        assert "0.0512345678" not in ctx.for_vol()

    def test_risk_veto_is_restated_in_plain_language(self):
        from app.commodity_agents.orchestrator import _plain_summary

        verdict = risk_guard.RiskVerdict(approved=False, reasons=[
            "correlated short-vega exposure: 1 underlying(s) in the energy "
            "group already carry short option positions — one macro shock "
            "would hit both",
            "no margin available",
        ])
        out = _plain_summary({"headline": "Sell the straddle.",
                              "plain_reasons": ["Options are expensive."]}, verdict)
        assert "safety checks" in out["headline"]
        assert len(out["plain_reasons"]) == 2
        assert all("short-vega" not in r and "margin available" not in r
                   for r in out["plain_reasons"])

    def test_capacity_gate_skips_the_debate_when_no_room(self):
        from unittest.mock import MagicMock

        from app.commodity_agents import orchestrator as orch

        session, settings, kite = MagicMock(), MagicMock(), MagicMock()
        settings.MAX_DAILY_LOSS_ABS = 10000.0
        settings.COMMODITY_MAX_CONCURRENT = 2
        # A segregated commodity ledger that is fully deployed: no free margin
        # left, but the segment is plainly in use, so it is the ledger to read.
        kite.margins.return_value = {
            "commodity": {"net": 0.0,
                          "available": {"live_balance": 250000.0},
                          "utilised": {"debits": 250000.0}},
            "equity": {"net": 900000.0},
        }
        with patch.object(orch, "_daily_realized_pnl", return_value=-12000.0), \
                patch.object(orch, "_open_commodity_count", return_value=3):
            out = orch._capacity_block(session, settings, kite, "GOLDM",
                                       datetime(2026, 7, 27, 20, 0, tzinfo=IST))
        assert len(out) == 3      # loss breaker + concurrency cap + zero margin

    def test_capacity_gate_clear_when_there_is_room(self):
        from unittest.mock import MagicMock

        from app.commodity_agents import orchestrator as orch

        session, settings, kite = MagicMock(), MagicMock(), MagicMock()
        settings.MAX_DAILY_LOSS_ABS = 10000.0
        settings.COMMODITY_MAX_CONCURRENT = 2
        kite.margins.return_value = {"commodity": {"net": 250000.0}}
        with patch.object(orch, "_daily_realized_pnl", return_value=-500.0), \
                patch.object(orch, "_open_commodity_count", return_value=0):
            assert orch._capacity_block(
                session, settings, kite, "GOLDM",
                datetime(2026, 7, 27, 20, 0, tzinfo=IST)) == []

    def test_a_flat_broker_book_never_reads_as_full(self):
        """Closed Position rows are never deleted, so a DB-only count grows
        monotonically and eventually pins the gate shut on a flat book."""
        from unittest.mock import MagicMock

        from app.commodity_agents import orchestrator as orch

        session, kite = MagicMock(), MagicMock()
        kite.positions.return_value = {"net": [
            {"exchange": "MCX", "tradingsymbol": "CRUDEOILM26AUG7500CE", "quantity": 0},
            {"exchange": "MCX", "tradingsymbol": "NATURALGAS26AUGFUT", "quantity": 0},
        ]}
        assert orch._open_commodity_count(session, kite) == 0
        session.query.assert_not_called()

    def test_open_count_is_per_underlying_not_per_leg(self):
        from unittest.mock import MagicMock

        from app.commodity_agents import orchestrator as orch

        session, kite = MagicMock(), MagicMock()
        kite.positions.return_value = {"net": [
            {"exchange": "MCX", "tradingsymbol": "CRUDEOILM26AUG7500CE", "quantity": -5},
            {"exchange": "MCX", "tradingsymbol": "CRUDEOILM26AUG8200PE", "quantity": -3},
            {"exchange": "NFO", "tradingsymbol": "BANKNIFTY26JUL57500CE", "quantity": 30},
            # CRUDEOIL is a prefix of CRUDEOILM — must not count as a third.
            {"exchange": "MCX", "tradingsymbol": "CRUDEOILM26AUGFUT", "quantity": 1},
        ]}
        assert orch._open_commodity_count(session, kite) == 2

    def test_group_shorts_read_short_legs_from_the_broker(self):
        from unittest.mock import MagicMock

        from app.commodity_agents import orchestrator as orch

        session, kite = MagicMock(), MagicMock()
        kite.positions.return_value = {"net": [
            {"exchange": "MCX", "tradingsymbol": "NATURALGAS26AUG300CE", "quantity": -1},
            {"exchange": "MCX", "tradingsymbol": "GOLDM26AUG80000CE", "quantity": -1},
        ]}
        # NATURALGAS shares the energy group with CRUDEOILM; GOLDM does not.
        assert orch._open_group_shorts(session, "CRUDEOILM", kite) == 1
        # A long leg is not short-vega exposure.
        kite.positions.return_value = {"net": [
            {"exchange": "MCX", "tradingsymbol": "NATURALGAS26AUG300CE", "quantity": 1},
        ]}
        assert orch._open_group_shorts(session, "CRUDEOILM", kite) == 0

    def test_pooled_ledger_is_not_mistaken_for_an_empty_account(self):
        """Unsegregated accounts report everything under equity; the commodity
        segment then reads all-zero even with MCX positions open."""
        from unittest.mock import MagicMock

        from app.commodity_agents import orchestrator as orch

        kite = MagicMock()
        kite.margins.return_value = {
            "equity": {"net": 1420867.55,
                       "available": {"live_balance": 1420867.55},
                       "utilised": {"debits": 787873.74}},
            "commodity": {"net": 0, "available": {"live_balance": 0},
                          "utilised": {"debits": 0}},
        }
        assert orch._margin_available(kite, "CRUDEOILM") == 1420867.55
        # A segregated commodity ledger with real usage is read as-is.
        kite.margins.return_value["commodity"] = {
            "net": 0.0, "available": {"live_balance": 250000.0},
            "utilised": {"debits": 250000.0},
        }
        assert orch._margin_available(kite, "CRUDEOILM") == 0.0

    def test_unmapped_veto_reason_survives_verbatim(self):
        from app.commodity_agents.orchestrator import _plain_summary

        verdict = risk_guard.RiskVerdict(approved=False,
                                         reasons=["some new guard fired"])
        out = _plain_summary({}, verdict)
        assert out["plain_reasons"] == ["some new guard fired"]

    def test_strike_payload_drops_unused_fields(self):
        ctx = self._ctx()
        # Per-row keys are abbreviated and the fields no agent reasons over
        # are dropped; expiry moves to the header so it isn't repeated per row.
        assert '"sym"' in ctx.strikes and '"tradingsymbol"' not in ctx.strikes
        for dropped in ("lot_units", "target_delta", "instrument_type"):
            assert dropped not in ctx.strikes
        assert ctx.strikes.count("2026-07-28") == 1


# ── strike engine ────────────────────────────────────────────────────────────

def _seed_instruments(session, underlying="GOLDM", fut_price=72000.0,
                      expiry=date(2026, 7, 28), step=100.0, n=8):
    token = 1000
    session.add(Instrument(
        instrument_token=token, exchange_token=token, tradingsymbol=f"{underlying}26JULFUT",
        name=underlying, expiry=date(2026, 7, 30), strike=None, tick_size=1.0,
        lot_size=1, instrument_type="FUT", segment="MCX", exchange="MCX",
    ))
    for i in range(-n, n + 1):
        k = fut_price + i * step
        for opt in ("CE", "PE"):
            token += 1
            session.add(Instrument(
                instrument_token=token, exchange_token=token,
                tradingsymbol=f"{underlying}26JUL{int(k)}{opt}",
                name=underlying, expiry=expiry, strike=k, tick_size=0.5,
                lot_size=1, instrument_type=opt, segment="MCX", exchange="MCX",
            ))
    session.commit()


def _synthetic_quote_fn(fut_price: float, iv: float, expiry: date):
    """Black-76 theoretical prices so compute_delta round-trips cleanly."""
    from py_vollib.black import black as b76_price

    def quote_fn(keys: list[str]) -> dict:
        out = {}
        t = max((datetime(expiry.year, expiry.month, expiry.day, 17, 0, tzinfo=IST)
                 - datetime(2026, 7, 5, 12, 0, tzinfo=IST)).total_seconds() / 31_557_600, 1e-4)
        for key in keys:
            sym = key.split(":", 1)[1]
            opt = sym[-2:]
            k = float(sym[len(sym.rstrip("0123456789CPE")):-2] or 0)
            # parse strike back out of the symbol
            digits = "".join(ch for ch in sym if ch.isdigit())
            k = float(digits[2:]) if len(digits) > 2 else float(digits)
            price = b76_price("c" if opt == "CE" else "p", fut_price, k, t, 0.07, iv)
            out[key] = {"last_price": round(max(price, 0.5), 2)}
        return out
    return quote_fn


class TestStrikes:
    def test_candidates_cover_deltas_both_sides(self, db_factory):
        with db_factory() as session:
            expiry = date(2026, 7, 28)
            _seed_instruments(session, expiry=expiry)
            now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
            cands = strikes.build_strike_candidates(
                session, _synthetic_quote_fn(72000.0, 0.15, expiry),
                "GOLDM", 72000.0, now=now,
            )
            sides = {c.instrument_type for c in cands}
            assert sides == {"CE", "PE"}
            deltas = {c.target_delta for c in cands}
            assert deltas == {0.25, 0.50, 0.65, 0.80}
            atm = [c for c in cands if c.target_delta == 0.50]
            for c in atm:
                assert abs(abs(c.delta) - 0.50) < 0.15

    def test_atm_iv_recovered(self, db_factory):
        with db_factory() as session:
            expiry = date(2026, 7, 28)
            _seed_instruments(session, expiry=expiry)
            now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
            cands = strikes.build_strike_candidates(
                session, _synthetic_quote_fn(72000.0, 0.15, expiry),
                "GOLDM", 72000.0, now=now,
            )
            iv = strikes.atm_iv_from_candidates(cands)
            assert iv is not None and abs(iv - 0.15) < 0.03

    def test_no_expiry_returns_empty(self, db_factory):
        with db_factory() as session:
            now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
            assert strikes.build_strike_candidates(
                session, lambda keys: {}, "SILVERM", 90000.0, now=now) == []


# ── orchestrator end-to-end with fake LLM ────────────────────────────────────

class FakeLlm:
    """Canned debate outputs; judge recommends a short straddle."""

    def __init__(self, gap_risk="low"):
        self.gap_risk = gap_risk

    def run(self, role, system, user):
        if role == "judge":
            return {"direction": "SELL", "strategy_type": "short_straddle",
                    "strikes": ["GOLDM26JUL72000CE", "GOLDM26JUL72000PE"],
                    "confidence": 0.8, "reasoning_summary": "IV rich, range-bound",
                    "dissenting_views": ["trend agent worried about breakout"]}
        flag = self.gap_risk if role == "event" else "low"
        return {"stance": "ok", "bull_case": "b", "bear_case": "s",
                "risk_flag": flag, "confidence": 0.7, "falsifier": "f"}


def _mk_kite(fut_price=72000.0, expiry=date(2026, 7, 28)):
    kite = MagicMock()
    closes = [72000.0 + (i % 7) * 3 for i in range(60)]
    kite.historical_data.return_value = _mk_candles(closes, spread=5.0)
    kite.ltp.return_value = {"MCX:GOLDM26JULFUT": {"last_price": fut_price}}
    kite.quote.side_effect = _synthetic_quote_fn(fut_price, 0.15, expiry)
    kite.margins.return_value = {"commodity": {"net": 800000.0}}
    return kite


@pytest.fixture
def ca_settings():
    # Raise the per-lot loss cap: a GOLDM ATM straddle at 100 units/lot has an
    # est. worst case ≈ ₹2.2L, which the default ₹15k cap correctly vetoes.
    from app.config import get_settings
    return get_settings().model_copy(update={"COMMODITY_MAX_LOSS_PER_LOT": 400000.0})


class TestOrchestrator:
    def test_full_run_produces_recommendation_and_trail(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_commodity_pipeline
        with db_factory() as s:
            _seed_instruments(s)
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)   # Sunday, no blackout
        run_id = run_commodity_pipeline(
            "GOLDM", db_factory, ca_settings, _mk_kite(), FakeLlm(), now=now)
        assert run_id is not None
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            assert run.status == "COMPLETED"
            for field in ("regime_json", "strikes_json", "trend_json",
                          "event_debate_json", "vol_json", "judge_json", "risk_json"):
                assert getattr(run, field), f"{field} missing"
            rec = s.query(CommodityRecommendation).filter_by(run_pk=run.id).one()
            assert rec.direction == "SELL"
            assert rec.strategy_type == "short_straddle"
            assert json.loads(rec.dissent_json)   # dissent preserved
            assert rec.status == "PROPOSED"

    def test_high_gap_risk_downgrades_to_no_trade(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_commodity_pipeline
        with db_factory() as s:
            _seed_instruments(s)
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_commodity_pipeline(
            "GOLDM", db_factory, ca_settings, _mk_kite(), FakeLlm(gap_risk="high"), now=now)
        with db_factory() as s:
            rec = s.query(CommodityRecommendation).one()
            assert rec.direction == "NO_TRADE"
            assert rec.strategy_type == "none"

    def test_failed_run_recorded_not_raised(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_commodity_pipeline
        kite = MagicMock()
        kite.historical_data.side_effect = RuntimeError("kite down")
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_commodity_pipeline(
            "GOLDM", db_factory, ca_settings, kite, FakeLlm(), now=now)
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            assert run.status == "FAILED"
            assert run.error


class FakeSingleCallLlm:
    """Stands in for ShimClient: one .run() call returns all four seats."""
    single_call = True

    def __init__(self, gap_risk="low", drop=None):
        self.gap_risk = gap_risk
        self.drop = drop            # simulate a section missing from the reply
        self.calls = []

    def run(self, role, system, user):
        self.calls.append((role, system, user))
        agent = {"stance": "ok", "bull_case": "b", "bear_case": "s",
                 "risk_flag": "low", "confidence": 0.7, "falsifier": "f"}
        out = {
            "trend": dict(agent),
            "event": dict(agent, risk_flag=self.gap_risk),
            "vol": dict(agent),
            "judge": {"direction": "SELL", "strategy_type": "short_straddle",
                      "strikes": ["GOLDM26JUL72000CE", "GOLDM26JUL72000PE"],
                      "confidence": 0.8, "reasoning_summary": "IV rich, range-bound",
                      "headline": "Options look pricey for how little this is moving.",
                      "plain_reasons": ["Priced for bigger swings than it makes."],
                      "dissenting_views": ["trend agent worried about breakout"]},
        }
        if self.drop:
            out.pop(self.drop)
        return out


class TestSingleCallDebate:
    """The shim backend collapses four roles into one call (each CLI
    invocation carries ~18.9K tokens of fixed scaffolding). Everything
    downstream must be unable to tell which backend ran."""

    def test_one_call_fills_every_stage_column(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_commodity_pipeline
        with db_factory() as s:
            _seed_instruments(s)
        llm = FakeSingleCallLlm()
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_commodity_pipeline("GOLDM", db_factory, ca_settings,
                                        _mk_kite(), llm, now=now)
        assert len(llm.calls) == 1, "the whole point is one invocation"
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            assert run.status == "COMPLETED"
            for field in ("trend_json", "event_debate_json", "vol_json", "judge_json"):
                assert json.loads(getattr(run, field)), f"{field} missing"
            rec = s.query(CommodityRecommendation).filter_by(run_pk=run.id).one()
            assert rec.direction == "SELL"
            assert rec.headline

    def test_gap_risk_gate_still_applies(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_commodity_pipeline
        with db_factory() as s:
            _seed_instruments(s)
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_commodity_pipeline(
            "GOLDM", db_factory, ca_settings, _mk_kite(),
            FakeSingleCallLlm(gap_risk="high"), now=now)
        with db_factory() as s:
            rec = s.query(CommodityRecommendation).filter_by(
                run_pk=s.get(AgentRun, run_id).id).one()
            assert rec.direction == "NO_TRADE"

    def test_missing_section_fails_loudly(self):
        from app.commodity_agents.llm_client import LlmError
        with pytest.raises(LlmError, match="judge"):
            debate.split_full_debate({"trend": {}, "event": {}, "vol": {}})

    def test_missing_section_is_not_a_silent_no_trade(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_commodity_pipeline
        with db_factory() as s:
            _seed_instruments(s)
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_commodity_pipeline(
            "GOLDM", db_factory, ca_settings, _mk_kite(),
            FakeSingleCallLlm(drop="judge"), now=now)
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            assert run.status == "FAILED"
            assert not s.query(CommodityRecommendation).count()

    def test_prompt_carries_all_four_seats_and_the_full_context(self):
        ctx = TestDebateContext()._ctx()
        llm = FakeSingleCallLlm()
        debate.run_full_debate(llm, ctx)
        _role, system, user = llm.calls[0]
        for seat in ("TREND/BREAKOUT", "EVENT/GAP-RISK", "VOLATILITY", "JUDGE"):
            assert seat in system
        assert user == ctx.for_judge()
        # The role briefs must be the same text both backends send, or the two
        # would grade the same setup differently.
        assert debate._TREND_BRIEF in system
        assert debate._EVENT_BRIEF in system
        assert debate._VOL_BRIEF in system
        assert debate._JUDGE_RULES in system


class TestShimClient:
    def _client(self):
        from app.commodity_agents.llm_shim import ShimClient
        return ShimClient(url="http://host.docker.internal:8765/",
                          token="s3cret", model="sonnet")

    def test_posts_bearer_token_and_parses_fenced_json(self):
        client = self._client()
        seen = {}

        def fake_post(payload):
            seen.update(payload)
            return {"result": '```json\n{"direction": "NO_TRADE"}\n```',
                    "usage": {"output_tokens": 42}, "total_cost_usd": 0.0}

        client._post = fake_post
        out = client.run("judge", "sys", "usr")
        assert out == {"direction": "NO_TRADE"}
        assert seen["system"] == "sys" and seen["user"] == "usr"
        assert seen["model"] == "sonnet"
        assert client.last_usage["output_tokens"] == 42

    def test_url_gets_the_run_path_once(self):
        assert self._client()._url == "http://host.docker.internal:8765/run"

    def test_web_search_only_where_it_is_needed(self):
        client = self._client()
        seen = []
        client._post = lambda p: (seen.append(p), {"result": "{}"})[1]
        for role in ("trend", "vol", "event", "judge"):
            client.run(role, "s", "u")
        assert [p["web_search"] for p in seen] == [False, False, True, True]

    def test_missing_token_refuses_to_construct(self):
        from app.commodity_agents.llm_client import LlmError
        from app.commodity_agents.llm_shim import ShimClient
        with pytest.raises(LlmError):
            ShimClient(url="http://x:1", token="")

    def test_transport_failure_is_not_retried(self):
        from app.commodity_agents.llm_client import LlmError
        client = self._client()
        calls = []

        def boom(_payload):
            calls.append(1)
            raise LlmError("shim call failed: connection refused")

        client._post = boom
        with pytest.raises(LlmError):
            client.run("judge", "s", "u")
        assert len(calls) == 1, "a dead shim must not burn a second CLI run"


class TestBackendSelection:
    def _settings(self, **over):
        from app.config import get_settings
        base = {"COMMODITY_AGENT_SHIM_URL": "http://host.docker.internal:8765",
                "COMMODITY_AGENT_SHIM_TOKEN": "tok",
                "ANTHROPIC_API_KEY": "sk-test"}
        base.update(over)
        return get_settings().model_copy(update=base)

    def test_scheduled_runs_stay_on_the_paid_api(self):
        from app.commodity_agents.orchestrator import make_backend
        client, name = make_backend(self._settings(), manual=False)
        assert name == "api"
        assert not getattr(client, "single_call", False)

    def test_manual_runs_prefer_the_shim(self):
        from app.commodity_agents.orchestrator import make_backend
        client, name = make_backend(self._settings(), manual=True)
        assert name == "shim"
        assert client.single_call is True

    def test_manual_falls_back_when_shim_unconfigured(self):
        from app.commodity_agents.orchestrator import make_backend
        _client, name = make_backend(
            self._settings(COMMODITY_AGENT_SHIM_TOKEN=""), manual=True)
        assert name == "api"

    def test_opting_out_keeps_manual_on_the_api(self):
        from app.commodity_agents.orchestrator import make_backend
        _client, name = make_backend(
            self._settings(COMMODITY_AGENT_MANUAL_BACKEND="api"), manual=True)
        assert name == "api"

    def test_a_dead_shim_never_silently_bills_the_api(self):
        """The point of this backend is to not spend API money. A configured
        shim that happens to be down must break the button, not open a bill."""
        from app.commodity_agents.llm_client import LlmError
        from app.commodity_agents.orchestrator import make_backend
        client, name = make_backend(self._settings(), manual=True)
        assert name == "shim"

        def dead(_payload):
            raise LlmError("shim call failed: connection refused")

        client._post = dead
        with pytest.raises(LlmError, match="connection refused"):
            client.run("judge", "s", "u")


class TestIvSample:
    """run_iv_sample: the LLM-free deterministic snapshot that feeds the
    Volatility Monitor chart on a faster cadence than the debate pipeline."""

    def test_populates_atm_iv_without_debate(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_iv_sample
        with db_factory() as s:
            _seed_instruments(s)
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_iv_sample("GOLDM", db_factory, ca_settings, _mk_kite(), now=now)
        assert run_id is not None
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            assert run.status == "IV_SAMPLE"
            assert run.atm_iv is not None
            assert run.regime_json
            assert run.analytics_json
            # no debate round for a bare IV sample
            assert run.judge_json is None
            assert run.trend_json is None
            assert s.query(CommodityRecommendation).filter_by(run_pk=run.id).count() == 0

    def test_failure_recorded_not_raised(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_iv_sample
        kite = MagicMock()
        kite.historical_data.side_effect = RuntimeError("kite down")
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_iv_sample("GOLDM", db_factory, ca_settings, kite, now=now)
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            assert run.status == "FAILED"
            assert run.error

    def test_feeds_iv_history_between_debate_cycles(self, db_factory, ca_settings):
        """The whole point: a sample taken between two debate runs shows up
        in _iv_history, giving the chart real resolution instead of one
        point per (cost-throttled) 120-min debate cycle."""
        from app.commodity_agents.orchestrator import _iv_history, run_iv_sample
        with db_factory() as s:
            _seed_instruments(s)
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_iv_sample("GOLDM", db_factory, ca_settings, _mk_kite(), now=now)
        with db_factory() as s:
            hist = _iv_history(s, "GOLDM")
        assert len(hist) == 1

    def test_run_all_skips_closed_markets(self, db_factory, ca_settings, monkeypatch):
        from app.commodity_agents import orchestrator
        with db_factory() as s:
            _seed_instruments(s)
        calls: list[str] = []
        monkeypatch.setattr(orchestrator, "run_iv_sample",
                             lambda commodity, *a, **k: calls.append(commodity))
        monkeypatch.setattr(orchestrator, "market_open",
                             lambda commodity, now: commodity == "GOLDM")
        monkeypatch.setattr("app.kite_session.get_session_manager",
                            lambda: MagicMock(get_kite=lambda: MagicMock()))
        orchestrator.run_all_iv_samples(db_factory, ca_settings)
        assert calls == ["GOLDM"]


# ── index underlyings (NIFTY / BANKNIFTY) ────────────────────────────────────

class TestIndexSupport:
    def test_exchange_routing(self):
        from app.commodity_agents.models import exchange_for
        assert exchange_for("NIFTY") == "NFO"
        assert exchange_for("BANKNIFTY") == "NFO"
        assert exchange_for("GOLDM") == "MCX"

    def test_rbi_hits_indices_not_commodities(self):
        from app.commodity_agents import events as ev
        now = datetime(2026, 2, 5, 12, 0, tzinfo=IST)   # day before Feb-6 RBI MPC
        nifty = ev.upcoming_events("NIFTY", now, horizon_hours=48)
        gold = ev.upcoming_events("GOLDM", now, horizon_hours=48)
        assert any("RBI" in e.name for e in nifty)
        assert not any("RBI" in e.name for e in gold)

    def test_fomc_hits_indices(self):
        from app.commodity_agents import events as ev
        now = datetime(2026, 7, 28, 12, 0, tzinfo=IST)
        assert any("FOMC" in e.name
                   for e in ev.upcoming_events("BANKNIFTY", now, horizon_hours=48))

    def test_market_open_gate(self):
        from app.commodity_agents.orchestrator import market_open
        tue_11am = datetime(2026, 7, 7, 11, 0, tzinfo=IST)
        tue_10pm = datetime(2026, 7, 7, 22, 0, tzinfo=IST)
        sat_11am = datetime(2026, 7, 11, 11, 0, tzinfo=IST)
        assert market_open("NIFTY", tue_11am) is True
        assert market_open("NIFTY", tue_10pm) is False      # NSE closed by 15:30
        assert market_open("CRUDEOILM", tue_10pm) is True    # MCX evening session
        assert market_open("CRUDEOILM", sat_11am) is False   # weekend

    def test_debate_profiles_exist(self):
        from app.commodity_agents.debate import COMMODITY_PROFILES
        from app.commodity_agents.models import COMMODITIES
        for u in COMMODITIES:
            assert u in COMMODITY_PROFILES

    def test_nfo_strike_candidates_use_bsm(self, db_factory):
        """NFO index options: real lot_size from the row, BSM greeks off spot."""
        from py_vollib.black_scholes_merton import black_scholes_merton as bsm_price
        spot, expiry = 25000.0, date(2026, 7, 28)
        with db_factory() as session:
            token = 5000
            for i in range(-6, 7):
                k = spot + i * 100
                for opt in ("CE", "PE"):
                    token += 1
                    session.add(Instrument(
                        instrument_token=token, exchange_token=token,
                        tradingsymbol=f"NIFTY26JUL{int(k)}{opt}",
                        name="NIFTY", expiry=expiry, strike=k, tick_size=0.05,
                        lot_size=75, instrument_type=opt, segment="NFO-OPT",
                        exchange="NFO",
                    ))
            session.commit()
            now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
            t = ((datetime(2026, 7, 28, 15, 30, tzinfo=IST) - now).total_seconds()
                 / 31_557_600)

            def quote_fn(keys):
                out = {}
                for key in keys:
                    sym = key.split(":", 1)[1]
                    opt = sym[-2:]
                    # digits = "26" (from 26JUL) + strike
                    k = float("".join(ch for ch in sym if ch.isdigit())[2:])
                    px = bsm_price("c" if opt == "CE" else "p",
                                   spot, k, t, 0.07, 0.12, 0.0)
                    out[key] = {"last_price": round(max(px, 0.5), 2)}
                return out

            cands = strikes.build_strike_candidates(session, quote_fn, "NIFTY", spot, now=now)
            assert {c.instrument_type for c in cands} == {"CE", "PE"}
            assert all(c.lot_units == 75 for c in cands)
            atm = [c for c in cands if c.target_delta == 0.50]
            assert atm and all(abs(abs(c.delta) - 0.5) < 0.15 for c in atm)


# ── CSV backtest loaders ─────────────────────────────────────────────────────

class TestCsvBacktest:
    def _write_futures_csv(self, tmp_path, underlying="NIFTY"):
        d = tmp_path / f"{underlying.lower()}_futures"
        d.mkdir()
        header = ("date,time,open,high,low,close,implied_fwd,expiry,"
                  "days_to_expiry,underlying,expiry_type,atm_strike,"
                  "ce_price,pe_price,n_strikes\n")
        # two files = two expiries covering the same date; near one must win
        near = d / "2026-07-28_monthly_5min.csv"
        far = d / "2026-08-25_monthly_5min.csv"
        rows_near, rows_far = [header], [header]
        for i, t in enumerate(["09:15", "09:30", "09:45", "12:00", "15:15"]):
            fwd = 25000 + i
            rows_near.append(f"2026-07-06,{t},1,1,1,1,{fwd},2026-07-28,22,"
                             f"{underlying},monthly,25000,200.0,190.0,50\n")
            rows_far.append(f"2026-07-06,{t},1,1,1,1,{fwd},2026-08-25,50,"
                            f"{underlying},monthly,25000,300.0,290.0,50\n")
        near.write_text("".join(rows_near))
        far.write_text("".join(rows_far))
        return str(d)

    def test_front_contract_selection(self, tmp_path):
        from app.commodity_agents.backtest import load_front_series
        series = load_front_series(self._write_futures_csv(tmp_path))
        day = series[date(2026, 7, 6)]
        assert day["days_to_expiry"] == 22            # near expiry chosen
        assert day["expiry"] == "2026-07-28"
        assert len(day["bars"]) == 5

    def test_strike_file_cache(self, tmp_path):
        from app.commodity_agents.backtest import _StrikeFileCache
        opt_dir = tmp_path / "nifty_options" / "2026-07"
        opt_dir.mkdir(parents=True)
        f = opt_dir / "NIFTY 25000 CE 28 JUL 26_5min.csv"
        f.write_text(
            "date,time,open,high,low,close,volume,oi,expiry,strike,option_type\n"
            "2026-07-06,09:45:00,1,1,1,210.0,0,0,2026-07-28,25000.0,CE\n"
            "2026-07-06,15:15:00,1,1,1,150.0,0,0,2026-07-28,25000.0,CE\n"
        )
        cache = _StrikeFileCache(str(tmp_path / "nifty_options"))
        px = cache.price_at("NIFTY", 25000.0, "CE", "2026-07-28",
                            date(2026, 7, 6), "15:15")
        assert px == 150.0
        assert cache.price_at("NIFTY", 26000.0, "CE", "2026-07-28",
                              date(2026, 7, 6), "15:15") is None


# ── vol analytics: VRP, IV trend, expected move, stress ─────────────────────

class TestAnalytics:
    def _atm_pair(self, F=72000.0, ce_ltp=800.0, pe_ltp=780.0, iv=0.15,
                  lot_units=100, expiry="2026-07-28"):
        from app.commodity_agents.models import StrikeCandidate
        mk = lambda side, ltp: StrikeCandidate(
            tradingsymbol=f"GOLDM26JUL72000{side}", instrument_type=side,
            strike=F, expiry=expiry, ltp=ltp, iv=iv, delta=0.5 if side == "CE" else -0.5,
            target_delta=0.50, lot_units=lot_units,
        )
        return [mk("CE", ce_ltp), mk("PE", pe_ltp)]

    def test_iv_trend_directions(self):
        from app.commodity_agents.analytics import iv_trend
        assert iv_trend([0.2] * 5)["direction"] == "insufficient-history"
        up = iv_trend([0.20] * 10 + [0.26, 0.27, 0.28])
        assert up["direction"] == "expanding" and up["change_pct"] > 0
        down = iv_trend([0.30] * 10 + [0.24, 0.23, 0.22])
        assert down["direction"] == "contracting" and down["change_pct"] < 0
        assert iv_trend([0.25] * 12)["direction"] == "stable"

    def test_vrp_and_expected_move(self):
        from app.commodity_agents.analytics import compute_analytics
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        out = compute_analytics(
            "GOLDM", self._atm_pair(), atm_iv=0.15, realized_vol=0.10,
            future_price=72000.0, now=now, iv_history_asc=[0.15] * 3,
        )
        assert out["vrp"]["vrp_pts"] == 5.0
        em = out["expected_move"]
        assert em["implied_move_pts"] == 1580.0            # CE + PE premium
        assert em["breakeven_upper"] == 72000.0 + 1580.0
        assert em["breakeven_lower"] == 72000.0 - 1580.0
        assert em["edge_ratio"] is not None and em["edge_ratio"] > 0
        assert 20 < em["days_to_expiry"] < 25

    def test_stress_table_shape_and_signs(self):
        from app.commodity_agents.analytics import compute_analytics
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        out = compute_analytics(
            "GOLDM", self._atm_pair(), atm_iv=0.15, realized_vol=0.10,
            future_price=72000.0, now=now, iv_history_asc=[],
        )
        rows = out["stress"]["rows"]
        assert len(rows) == 7
        flat = next(r for r in rows if r["move_pct"] == 0.0)
        # no move, held to expiry: full credit kept
        assert flat["pnl_at_expiry"] == round(1580.0 * 100)
        # instant +5 IV pts with no move hurts the short straddle
        assert flat["pnl_iv_up5"] < 0
        worst_up = next(r for r in rows if r["move_pct"] == 3.0)
        # 3% move = 2160 pts > 1580 credit → loss at expiry
        assert worst_up["pnl_at_expiry"] < 0

    def test_partial_inputs_degrade_gracefully(self):
        from app.commodity_agents.analytics import compute_analytics
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        out = compute_analytics(
            "GOLDM", [], atm_iv=None, realized_vol=None,
            future_price=None, now=now, iv_history_asc=[],
        )
        assert "vrp" not in out and "expected_move" not in out
        assert out["iv_trend"]["direction"] == "insufficient-history"

    def test_orchestrator_persists_analytics(self, db_factory, ca_settings):
        from app.commodity_agents.orchestrator import run_commodity_pipeline
        with db_factory() as s:
            _seed_instruments(s)
        now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
        run_id = run_commodity_pipeline(
            "GOLDM", db_factory, ca_settings, _mk_kite(), FakeLlm(), now=now)
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            quant = json.loads(run.analytics_json)
            assert "vrp" in quant and "stress" in quant
            assert len(quant["stress"]["rows"]) == 7

    def test_margin_required_basket(self):
        from app.commodity_agents.orchestrator import _margin_required_basket
        from app.commodity_agents.models import StrikeCandidate
        ce = StrikeCandidate("GOLDM26JUL72000CE", "CE", 72000.0, "2026-07-28",
                             800.0, 0.15, 0.5, 0.50, 100)
        pe = StrikeCandidate("GOLDM26JUL72000PE", "PE", 72000.0, "2026-07-28",
                             780.0, 0.15, -0.5, 0.50, 100)
        kite = MagicMock()
        kite.basket_order_margins.return_value = {"final": {"total": 123456.0}}
        assert _margin_required_basket(kite, "GOLDM", ce, pe) == 123456.0
        legs = kite.basket_order_margins.call_args[0][0]
        assert all(l["transaction_type"] == "SELL" for l in legs)
        assert legs[0]["quantity"] == 1          # MCX: quantity = lots
        # NFO: quantity = lots × lot_size
        kite.basket_order_margins.return_value = {"final": {"total": 90000.0}}
        ce_n = StrikeCandidate("NIFTY26JUL25000CE", "CE", 25000.0, "2026-07-28",
                               200.0, 0.12, 0.5, 0.50, 75)
        pe_n = StrikeCandidate("NIFTY26JUL25000PE", "PE", 25000.0, "2026-07-28",
                               190.0, 0.12, -0.5, 0.50, 75)
        assert _margin_required_basket(kite, "NIFTY", ce_n, pe_n) == 90000.0
        assert kite.basket_order_margins.call_args[0][0][0]["quantity"] == 75
        # broker failure → None (risk guard then skips the margin check)
        kite.basket_order_margins.side_effect = RuntimeError("api down")
        assert _margin_required_basket(kite, "GOLDM", ce, pe) is None


# ── chain positioning (OI), skew, term structure, iron fly, correlation ─────

class TestChainPositioning:
    def test_pcr_walls_max_pain(self, db_factory):
        with db_factory() as session:
            expiry = date(2026, 7, 28)
            _seed_instruments(session, expiry=expiry)
            base_fn = _synthetic_quote_fn(72000.0, 0.15, expiry)

            def quote_with_oi(keys):
                out = base_fn(keys)
                for key, q in out.items():
                    sym = key.split(":", 1)[1]
                    opt = sym[-2:]
                    digits = "".join(ch for ch in sym if ch.isdigit())
                    k = float(digits[2:])
                    if opt == "CE":
                        q["oi"] = 5000.0 if k == 72500.0 else 100.0
                    else:
                        q["oi"] = 8000.0 if k == 71500.0 else 100.0
                return out

            now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
            cands, pos = strikes.build_chain_snapshot(
                session, quote_with_oi, "GOLDM", 72000.0, now=now)
            assert cands
            assert pos["call_wall"] == 72500.0
            assert pos["put_wall"] == 71500.0
            assert pos["pcr"] > 1.0                      # PE OI heavier
            assert 71500.0 <= pos["max_pain"] <= 72500.0

    def test_no_oi_gives_empty_positioning(self, db_factory):
        with db_factory() as session:
            expiry = date(2026, 7, 28)
            _seed_instruments(session, expiry=expiry)
            now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
            _, pos = strikes.build_chain_snapshot(
                session, _synthetic_quote_fn(72000.0, 0.15, expiry),
                "GOLDM", 72000.0, now=now)
            assert pos == {}


class TestSkewTermIronFly:
    def _cand(self, side, strike, ltp, iv, td, lot_units=100):
        from app.commodity_agents.models import StrikeCandidate
        return StrikeCandidate(f"GOLDM26JUL{int(strike)}{side}", side, strike,
                               "2026-07-28", ltp, iv, 0.25 if td == 0.25 else 0.5,
                               td, lot_units)

    def test_skew_put_richer(self):
        from app.commodity_agents.analytics import skew_25d
        cands = [self._cand("CE", 74000, 250, 0.14, 0.25),
                 self._cand("PE", 70000, 230, 0.18, 0.25)]
        sk = skew_25d(cands)
        assert sk["rr_25d_pts"] == pytest.approx(4.0)
        assert "downside" in sk["read"]

    def test_skew_needs_both_wings(self):
        from app.commodity_agents.analytics import skew_25d
        assert skew_25d([self._cand("CE", 74000, 250, 0.14, 0.25)]) is None

    def test_term_structure_reads(self):
        from app.commodity_agents.analytics import term_structure
        inv = term_structure(0.20, 0.16)
        assert inv["ratio"] > 1.05 and "inverted" in inv["read"]
        cont = term_structure(0.15, 0.17)
        assert cont["ratio"] < 0.95 and "contango" in cont["read"]
        assert term_structure(None, 0.2) is None

    def test_iron_fly_capped_loss(self):
        from app.commodity_agents.orchestrator import _iron_fly_max_loss
        atm_ce = self._cand("CE", 72000, 800, 0.15, 0.50)
        atm_pe = self._cand("PE", 72000, 780, 0.15, 0.50)
        wings = [self._cand("CE", 74000, 250, 0.14, 0.25),
                 self._cand("PE", 70000, 230, 0.16, 0.25)]
        # width 2000, net credit (800+780)-(250+230)=1100 → (2000-1100)×100
        loss = _iron_fly_max_loss([atm_ce, atm_pe] + wings, atm_ce, atm_pe)
        assert loss == pytest.approx(90000.0)
        # missing wings → None (caller keeps the bigger naked estimate)
        assert _iron_fly_max_loss([atm_ce, atm_pe], atm_ce, atm_pe) is None


class TestCorrelationGuard:
    def _limits(self):
        return risk_guard.RiskLimits(
            max_loss_per_lot=500000, max_daily_loss=10000,
            max_concurrent_commodities=5, max_margin_util_pct=60, max_lots=5)

    def _inp(self, **kw):
        base = dict(commodity="CRUDEOILM", strategy_type="short_straddle", lots=1,
                    est_max_loss_per_lot=1000.0, daily_realized_pnl=0.0,
                    open_commodity_count=0, margin_available=None,
                    margin_required=None, blackout=None)
        base.update(kw)
        return risk_guard.RiskInput(**base)

    def test_group_short_vetoes(self):
        v = risk_guard.evaluate(self._limits(),
                                self._inp(open_group_shorts=1, group_name="energy"))
        assert not v.approved
        assert any("correlated" in r for r in v.reasons)

    def test_long_option_not_blocked_by_group(self):
        v = risk_guard.evaluate(self._limits(),
                                self._inp(strategy_type="long_call",
                                          open_group_shorts=1, group_name="energy"))
        assert v.approved

    def test_group_mapping(self):
        from app.commodity_agents.models import group_for
        assert group_for("NATURALGAS") == "energy"
        assert group_for("SILVERM") == "metals"
        assert group_for("BANKNIFTY") == "indices"
        assert group_for("UNKNOWN") is None


class TestCalendarRefresh:
    def test_validate_rules(self):
        from app.commodity_agents.calendar_refresh import _validate
        now = datetime(2026, 7, 6, 10, 0, tzinfo=IST)
        good = {"name": "RBI MPC", "commodities": ["NIFTY", "BANKNIFTY"],
                "datetime_ist": "2026-08-06T10:00", "impact": "high"}
        assert _validate(good, now)["name"] == "RBI MPC"
        assert _validate({**good, "commodities": ["DOGE"]}, now) is None
        assert _validate({**good, "impact": "extreme"}, now) is None
        assert _validate({**good, "datetime_ist": "not-a-date"}, now) is None
        assert _validate({**good, "datetime_ist": "2027-01-01T10:00"}, now) is None

    def test_refresh_writes_auto_events_and_calendar_reads_them(self, tmp_path, monkeypatch):
        from app.commodity_agents import calendar_refresh, events as events_mod
        from app.config import get_settings
        path = str(tmp_path / "commodity_events.json")
        monkeypatch.setattr(calendar_refresh, "EVENTS_OVERRIDE_PATH", path)
        monkeypatch.setattr(events_mod, "EVENTS_OVERRIDE_PATH", path)
        # pre-existing manual entry must survive the refresh
        (tmp_path / "commodity_events.json").write_text(json.dumps(
            {"extra_events": [{"name": "manual CPI", "commodities": ["GOLDM"],
                               "datetime_ist": "2026-07-14T18:00", "impact": "high"}]}))

        class FakeLlm:
            def __init__(self, **kw): pass
            def run(self, role, system, user):
                return {"events": [
                    {"name": "OPEC+ meeting", "commodities": ["CRUDEOILM"],
                     "datetime_ist": "2026-07-20T17:30", "impact": "high"},
                    {"name": "garbage", "commodities": ["DOGE"],
                     "datetime_ist": "2026-07-20T17:30", "impact": "high"},
                ]}
        monkeypatch.setattr(calendar_refresh, "LlmClient", FakeLlm)
        settings = get_settings().model_copy(update={
            "ANTHROPIC_API_KEY": "test-key", "COMMODITY_AGENTS_WEB_SEARCH": True})
        now = datetime(2026, 7, 6, 10, 0, tzinfo=IST)
        assert calendar_refresh.refresh_event_calendar(settings, now=now) == 1

        data = json.loads((tmp_path / "commodity_events.json").read_text())
        assert len(data["auto_events"]) == 1
        assert data["extra_events"][0]["name"] == "manual CPI"   # preserved
        evs = events_mod.upcoming_events("CRUDEOILM", now, horizon_hours=400)
        assert any(e.name == "OPEC+ meeting" for e in evs)
        gold = events_mod.upcoming_events("GOLDM", now, horizon_hours=400)
        assert any(e.name == "manual CPI" for e in gold)


class TestLlmPauseTurn:
    def _client_returning(self, responses):
        from types import SimpleNamespace
        calls = []

        class FakeMessages:
            def create(self, **kw):
                calls.append(kw)
                return responses[len(calls) - 1]

        return SimpleNamespace(messages=FakeMessages()), calls

    def test_pause_turn_is_continued(self):
        from types import SimpleNamespace
        from app.commodity_agents.llm_client import LlmClient
        paused = SimpleNamespace(
            stop_reason="pause_turn",
            content=[SimpleNamespace(type="server_tool_use")])
        final = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"risk_flag": "low"}')])
        llm = LlmClient(api_key="k", role_models={"event": "m"})
        llm._client, calls = self._client_returning([paused, final])
        out = llm.run("event", "sys", "ctx")
        assert out == {"risk_flag": "low"}
        assert len(calls) == 2
        # continuation echoes the paused content back as an assistant turn
        assert calls[1]["messages"][1]["role"] == "assistant"

    def test_no_text_after_max_turns_raises(self):
        from types import SimpleNamespace
        from app.commodity_agents.llm_client import LlmClient, LlmError
        paused = SimpleNamespace(stop_reason="pause_turn",
                                 content=[SimpleNamespace(type="server_tool_use")])
        llm = LlmClient(api_key="k", role_models={"event": "m"})
        llm._client, _ = self._client_returning([paused] * 5)
        with pytest.raises(LlmError, match="no text content"):
            llm.run("event", "sys", "ctx")


class TestJsonReplyParsing:
    def test_prose_around_json(self):
        from app.commodity_agents.llm_client import _parse_json_reply
        raw = 'Based on my search:\n{"risk_flag": "high", "confidence": 0.7}\nHope this helps.'
        assert _parse_json_reply(raw)["risk_flag"] == "high"

    def test_fenced_json(self):
        from app.commodity_agents.llm_client import _parse_json_reply
        assert _parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fence_missing_close_but_complete_object(self):
        from app.commodity_agents.llm_client import _parse_json_reply
        assert _parse_json_reply('```json\n{"a": 1}') == {"a": 1}

    def test_truly_truncated_still_raises(self):
        import json as _json
        from app.commodity_agents.llm_client import _parse_json_reply
        with pytest.raises(_json.JSONDecodeError):
            _parse_json_reply('```json\n{"stance": "cut off mid')

    def test_unparseable_reply_retried_with_strict_instruction(self):
        from types import SimpleNamespace
        from app.commodity_agents.llm_client import LlmClient
        bad = SimpleNamespace(stop_reason="end_turn",
                              content=[SimpleNamespace(type="text",
                                                       text='```json\n{"stance": "cut off mid')])
        good = SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text",
                                                        text='{"risk_flag": "medium"}')])
        llm = LlmClient(api_key="k", role_models={"event": "m"})
        llm._client, calls = self._client_returning([bad, good])
        assert llm.run("event", "sys", "ctx") == {"risk_flag": "medium"}
        assert len(calls) == 2
        assert "CRITICAL OUTPUT RULE" in calls[1]["system"]

    def _client_returning(self, responses):
        from types import SimpleNamespace
        calls = []

        class FakeMessages:
            def create(self, **kw):
                calls.append(kw)
                return responses[len(calls) - 1]

        return SimpleNamespace(messages=FakeMessages()), calls
