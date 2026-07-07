"""Tests for the commodity debate-agent pipeline: events, regime, risk guard,
strikes, gap-risk gate, and the end-to-end orchestrator with a fake LLM."""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

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
        evs = events.upcoming_events("GOLD", now, horizon_hours=96)
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
        gold = events.upcoming_events("GOLD", now, horizon_hours=48)
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


# ── risk guard ───────────────────────────────────────────────────────────────

def _limits(**kw):
    base = dict(max_loss_per_lot=15000, max_daily_loss=10000,
                max_concurrent_commodities=2, max_margin_util_pct=60, max_lots=5)
    base.update(kw)
    return risk_guard.RiskLimits(**base)


def _inp(**kw):
    base = dict(commodity="GOLD", strategy_type="short_straddle", lots=1,
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


# ── strike engine ────────────────────────────────────────────────────────────

def _seed_instruments(session, underlying="GOLD", fut_price=72000.0,
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
                "GOLD", 72000.0, now=now,
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
                "GOLD", 72000.0, now=now,
            )
            iv = strikes.atm_iv_from_candidates(cands)
            assert iv is not None and abs(iv - 0.15) < 0.03

    def test_no_expiry_returns_empty(self, db_factory):
        with db_factory() as session:
            now = datetime(2026, 7, 5, 12, 0, tzinfo=IST)
            assert strikes.build_strike_candidates(
                session, lambda keys: {}, "SILVER", 90000.0, now=now) == []


# ── orchestrator end-to-end with fake LLM ────────────────────────────────────

class FakeLlm:
    """Canned debate outputs; judge recommends a short straddle."""

    def __init__(self, gap_risk="low"):
        self.gap_risk = gap_risk

    def run(self, role, system, user):
        if role == "judge":
            return {"direction": "SELL", "strategy_type": "short_straddle",
                    "strikes": ["GOLD26JUL72000CE", "GOLD26JUL72000PE"],
                    "confidence": 0.8, "reasoning_summary": "IV rich, range-bound",
                    "dissenting_views": ["trend agent worried about breakout"]}
        flag = self.gap_risk if role == "event" else "low"
        return {"stance": "ok", "bull_case": "b", "bear_case": "s",
                "risk_flag": flag, "confidence": 0.7, "falsifier": "f"}


def _mk_kite(fut_price=72000.0, expiry=date(2026, 7, 28)):
    kite = MagicMock()
    closes = [72000.0 + (i % 7) * 3 for i in range(60)]
    kite.historical_data.return_value = _mk_candles(closes, spread=5.0)
    kite.ltp.return_value = {"MCX:GOLD26JULFUT": {"last_price": fut_price}}
    kite.quote.side_effect = _synthetic_quote_fn(fut_price, 0.15, expiry)
    kite.margins.return_value = {"commodity": {"net": 800000.0}}
    return kite


@pytest.fixture
def ca_settings():
    # Raise the per-lot loss cap: a GOLD ATM straddle at 100 units/lot has an
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
            "GOLD", db_factory, ca_settings, _mk_kite(), FakeLlm(), now=now)
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
            "GOLD", db_factory, ca_settings, _mk_kite(), FakeLlm(gap_risk="high"), now=now)
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
            "GOLD", db_factory, ca_settings, kite, FakeLlm(), now=now)
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            assert run.status == "FAILED"
            assert run.error


# ── index underlyings (NIFTY / BANKNIFTY) ────────────────────────────────────

class TestIndexSupport:
    def test_exchange_routing(self):
        from app.commodity_agents.models import exchange_for
        assert exchange_for("NIFTY") == "NFO"
        assert exchange_for("BANKNIFTY") == "NFO"
        assert exchange_for("GOLD") == "MCX"

    def test_rbi_hits_indices_not_commodities(self):
        from app.commodity_agents import events as ev
        now = datetime(2026, 2, 5, 12, 0, tzinfo=IST)   # day before Feb-6 RBI MPC
        nifty = ev.upcoming_events("NIFTY", now, horizon_hours=48)
        gold = ev.upcoming_events("GOLD", now, horizon_hours=48)
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
        assert market_open("CRUDEOIL", tue_10pm) is True    # MCX evening session
        assert market_open("CRUDEOIL", sat_11am) is False   # weekend

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
            tradingsymbol=f"GOLD26JUL72000{side}", instrument_type=side,
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
            "GOLD", self._atm_pair(), atm_iv=0.15, realized_vol=0.10,
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
            "GOLD", self._atm_pair(), atm_iv=0.15, realized_vol=0.10,
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
            "GOLD", [], atm_iv=None, realized_vol=None,
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
            "GOLD", db_factory, ca_settings, _mk_kite(), FakeLlm(), now=now)
        with db_factory() as s:
            run = s.get(AgentRun, run_id)
            quant = json.loads(run.analytics_json)
            assert "vrp" in quant and "stress" in quant
            assert len(quant["stress"]["rows"]) == 7

    def test_margin_required_basket(self):
        from app.commodity_agents.orchestrator import _margin_required_basket
        from app.commodity_agents.models import StrikeCandidate
        ce = StrikeCandidate("GOLD26JUL72000CE", "CE", 72000.0, "2026-07-28",
                             800.0, 0.15, 0.5, 0.50, 100)
        pe = StrikeCandidate("GOLD26JUL72000PE", "PE", 72000.0, "2026-07-28",
                             780.0, 0.15, -0.5, 0.50, 100)
        kite = MagicMock()
        kite.basket_order_margins.return_value = {"final": {"total": 123456.0}}
        assert _margin_required_basket(kite, "GOLD", ce, pe) == 123456.0
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
        assert _margin_required_basket(kite, "GOLD", ce, pe) is None


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
                session, quote_with_oi, "GOLD", 72000.0, now=now)
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
                "GOLD", 72000.0, now=now)
            assert pos == {}


class TestSkewTermIronFly:
    def _cand(self, side, strike, ltp, iv, td, lot_units=100):
        from app.commodity_agents.models import StrikeCandidate
        return StrikeCandidate(f"GOLD26JUL{int(strike)}{side}", side, strike,
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
        base = dict(commodity="CRUDEOIL", strategy_type="short_straddle", lots=1,
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
        assert group_for("SILVER") == "metals"
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
            {"extra_events": [{"name": "manual CPI", "commodities": ["GOLD"],
                               "datetime_ist": "2026-07-14T18:00", "impact": "high"}]}))

        class FakeLlm:
            def __init__(self, **kw): pass
            def run(self, role, system, user):
                return {"events": [
                    {"name": "OPEC+ meeting", "commodities": ["CRUDEOIL"],
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
        evs = events_mod.upcoming_events("CRUDEOIL", now, horizon_hours=400)
        assert any(e.name == "OPEC+ meeting" for e in evs)
        gold = events_mod.upcoming_events("GOLD", now, horizon_hours=400)
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
