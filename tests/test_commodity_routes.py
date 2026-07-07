"""Tests for commodity-agent Phase 4-6: notify gating, dashboard assets,
two-step decision flow, and the live-execution gate."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import storage as app_storage
from app.commodity_agents import notify, routes
from app.commodity_agents.routes import DecisionRequest, decide
from app.commodity_agents.storage import (
    AgentRun,
    CommodityRecommendation,
    RecommendationDecision,
)
from app.config import IST, get_settings
from app.storage import Base

TOKEN = "test-admin-token"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    old = app_storage._factory
    app_storage._factory = factory
    yield factory
    app_storage._factory = old


@pytest.fixture
def settings_paper(monkeypatch):
    s = get_settings().model_copy(update={"ADMIN_AUTH_TOKEN": TOKEN})
    monkeypatch.setattr(routes, "get_settings", lambda: s)
    return s


@pytest.fixture
def settings_live(monkeypatch):
    s = get_settings().model_copy(update={
        "ADMIN_AUTH_TOKEN": TOKEN, "COMMODITY_AGENTS_LIVE": True,
    })
    monkeypatch.setattr(routes, "get_settings", lambda: s)
    return s


def _seed_rec(factory, strategy="short_straddle", direction="SELL",
              strikes=("GOLD26JUL72000CE", "GOLD26JUL72000PE"),
              vetoed=False, status="PROPOSED") -> int:
    now = datetime.now(IST)
    with factory() as s:
        run = AgentRun(run_id="r-1", commodity="GOLD", started_at=now, status="COMPLETED")
        s.add(run)
        s.commit()
        rec = CommodityRecommendation(
            run_pk=run.id, commodity="GOLD", created_at=now,
            direction=direction, strategy_type=strategy,
            strikes_json=json.dumps(list(strikes)), confidence=0.8,
            reasoning_summary="test", dissent_json="[]",
            risk_vetoed=vetoed, status=status,
        )
        s.add(rec)
        s.commit()
        return rec.id


def _two_step(rec_id: int, action: str, lots: int = 1) -> dict:
    bg = BackgroundTasks()
    step1 = decide(DecisionRequest(recommendation_id=rec_id, action=action),
                   bg, x_admin_auth_token=TOKEN)
    assert step1["status"] == "confirm_required"
    return decide(
        DecisionRequest(recommendation_id=rec_id, action=action,
                        confirm_token=step1["confirm_token"], lots=lots),
        bg, x_admin_auth_token=TOKEN,
    ), bg


# ── notify ───────────────────────────────────────────────────────────────────

class TestNotify:
    def _settings(self, **kw):
        base = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}
        base.update(kw)
        return get_settings().model_copy(update=base)

    def test_noop_without_credentials(self):
        s = self._settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
        with patch.object(notify.requests, "post") as mock_post:
            assert notify.send_telegram(s, "hi") is False
            mock_post.assert_not_called()

    def test_no_trade_not_pushed(self):
        with patch.object(notify.requests, "post") as mock_post:
            out = notify.notify_recommendation(self._settings(), {
                "commodity": "GOLD", "direction": "NO_TRADE",
                "strategy_type": "none", "confidence": 0.99,
            })
            assert out is False
            mock_post.assert_not_called()

    def test_low_confidence_not_pushed(self):
        with patch.object(notify.requests, "post") as mock_post:
            out = notify.notify_recommendation(self._settings(), {
                "commodity": "GOLD", "direction": "SELL",
                "strategy_type": "short_straddle", "confidence": 0.3,
            })
            assert out is False
            mock_post.assert_not_called()

    def test_actionable_pushed(self):
        with patch.object(notify.requests, "post") as mock_post:
            mock_post.return_value.status_code = 200
            out = notify.notify_recommendation(self._settings(), {
                "id": 7, "commodity": "GOLD", "direction": "SELL",
                "strategy_type": "short_straddle", "confidence": 0.8,
                "strikes": ["A", "B"], "reasoning_summary": "iv rich",
            })
            assert out is True
            payload = mock_post.call_args.kwargs["json"]
            assert "GOLD" in payload["text"] and "SELL" in payload["text"]


# ── dashboard assets ─────────────────────────────────────────────────────────

class TestDashboardAssets:
    def test_dashboard_html(self):
        html = routes.dashboard_page()
        assert "Commodity Agents" in html
        assert "X-Admin-Auth-Token" not in html      # no credential in the browser
        assert "/login?next=" in html                # 401 → session sign-in
        assert "TAP AGAIN to confirm" in html        # two-step UI present

    def test_manifest(self):
        resp = routes.manifest()
        data = json.loads(resp.body)
        assert data["start_url"] == "/commodity-agents/dashboard"
        assert data["display"] == "standalone"

    def test_service_worker(self):
        resp = routes.service_worker()
        assert resp.media_type == "application/javascript"


# ── decision flow ────────────────────────────────────────────────────────────

class TestDecisionFlow:
    def test_requires_auth(self, db_factory, settings_paper):
        with pytest.raises(HTTPException) as e:
            decide(DecisionRequest(recommendation_id=1, action="approve"),
                   BackgroundTasks(), x_admin_auth_token="wrong")
        assert e.value.status_code == 401

    def test_two_step_reject(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory)
        result, _ = _two_step(rec_id, "reject")
        assert result["status"] == "REJECTED"
        assert result["executed"] is False
        with db_factory() as s:
            assert s.query(RecommendationDecision).count() == 1

    def test_bad_token_rejected(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory)
        bg = BackgroundTasks()
        decide(DecisionRequest(recommendation_id=rec_id, action="approve"),
               bg, x_admin_auth_token=TOKEN)
        with pytest.raises(HTTPException) as e:
            decide(DecisionRequest(recommendation_id=rec_id, action="approve",
                                   confirm_token="garbage"),
                   bg, x_admin_auth_token=TOKEN)
        assert e.value.status_code == 410

    def test_vetoed_cannot_be_approved(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory, vetoed=True)
        with pytest.raises(HTTPException) as e:
            decide(DecisionRequest(recommendation_id=rec_id, action="approve"),
                   BackgroundTasks(), x_admin_auth_token=TOKEN)
        assert e.value.status_code == 409

    def test_already_decided_conflict(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory, status="APPROVED")
        with pytest.raises(HTTPException) as e:
            decide(DecisionRequest(recommendation_id=rec_id, action="approve"),
                   BackgroundTasks(), x_admin_auth_token=TOKEN)
        assert e.value.status_code == 409


# ── execution gate (Phase 6) ─────────────────────────────────────────────────

class TestExecutionGate:
    def test_paper_mode_records_only(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory)
        result, bg = _two_step(rec_id, "approve")
        assert result["status"] == "APPROVED"
        assert result["executed"] is False
        assert "paper mode" in result["note"]
        assert bg.tasks == []

    def test_live_short_straddle_queues_process(self, db_factory, settings_live):
        rec_id = _seed_rec(db_factory)
        with patch("app.main._process_straddle") as mock_ps:
            result, bg = _two_step(rec_id, "approve", lots=2)
            assert result["executed"] is True
            assert len(bg.tasks) == 1
            # BackgroundTasks stores (func, args, kwargs); alert_id + entry_data
            task = bg.tasks[0]
            assert task.func is mock_ps
            entry_data = task.args[1]
            assert entry_data["_straddle_ce_symbol"].endswith("CE")
            assert entry_data["_straddle_pe_symbol"].endswith("PE")
            assert entry_data["quantity"] == 2
        from app.storage import Alert
        with db_factory() as s:
            alert = s.query(Alert).one()
            assert alert.strategy_id == f"commodity_agent_{rec_id}"

    def test_live_lots_clamped_to_cap(self, db_factory, settings_live):
        rec_id = _seed_rec(db_factory)
        with patch("app.main._process_straddle"):
            result, bg = _two_step(rec_id, "approve", lots=99)
            entry_data = bg.tasks[0].args[1]
            assert entry_data["quantity"] == settings_live_cap()

    def test_live_non_straddle_not_executed(self, db_factory, settings_live):
        rec_id = _seed_rec(db_factory, strategy="long_call",
                           strikes=("GOLD26JUL73000CE",))
        result, bg = _two_step(rec_id, "approve")
        assert result["executed"] is False
        assert "manually" in result["note"]
        assert bg.tasks == []


def settings_live_cap() -> int:
    return get_settings().COMMODITY_AGENT_MAX_LOTS


# ── backtest helpers ─────────────────────────────────────────────────────────

class TestBacktest:
    def test_replay_day_measures_adverse_move(self):
        from app.commodity_agents.backtest import replay_day
        upto = [{"high": 100.5, "low": 99.5, "close": 100.0} for _ in range(40)]
        after = [
            {"high": 101.0, "low": 99.0, "close": 100.5},
            {"high": 103.0, "low": 100.0, "close": 102.0},   # 3% spike up
        ]
        row = replay_day(upto, after, "GOLD",
                         datetime(2026, 7, 6, 21, 0, tzinfo=IST), 3.0, 1.0)
        assert row is not None
        assert row["adverse_move_pct"] == pytest.approx(3.0)
        assert row["regime"] in ("range-bound", "trending",
                                 "pre-event-compression", "high-vol-expansion")

    def test_replay_day_insufficient_data(self):
        from app.commodity_agents.backtest import replay_day
        assert replay_day([], [], "GOLD",
                          datetime(2026, 7, 6, 21, 0, tzinfo=IST), 3, 1) is None

    def test_summarize_groups_by_regime(self):
        from app.commodity_agents.backtest import summarize
        rows = [
            {"regime": "range-bound", "adverse_move_pct": 0.5, "blackout": ""},
            {"regime": "range-bound", "adverse_move_pct": 0.7, "blackout": ""},
            {"regime": "trending", "adverse_move_pct": 2.5, "blackout": "EIA"},
        ]
        out = summarize(rows)
        assert "range-bound" in out and "trending" in out


# ── analyze page + run-status endpoint ───────────────────────────────────────

class TestAnalyzeSupport:
    def test_analyze_page_served(self):
        html = routes.analyze_page()
        assert "Analyze a ticker" in html
        assert "NATURALGAS" in html and "BANKNIFTY" in html
        assert "/runs/latest" in html          # progress polling wired

    def test_latest_run_reports_stages(self, db_factory, settings_paper):
        now = datetime.now(IST)
        with db_factory() as s:
            run = AgentRun(run_id="r-stages", commodity="NIFTY", started_at=now,
                           status="RUNNING", events_json="[]", strikes_json="[]",
                           regime_json="{}", trend_json="{}")
            s.add(run)
            s.commit()
        out = routes.latest_run("NIFTY", x_admin_auth_token=TOKEN)
        stages = {st["key"]: st["done"] for st in out["run"]["stages"]}
        assert stages["events"] and stages["strikes"] and stages["regime"]
        assert stages["trend"] and not stages["gap_risk"] and not stages["judge"]
        assert out["run"]["status"] == "RUNNING"
        assert out["run"]["recommendation"] is None

    def test_latest_run_includes_recommendation_when_done(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory)   # seeds run r-1 + PROPOSED rec
        out = routes.latest_run("GOLD", x_admin_auth_token=TOKEN)
        assert out["run"]["recommendation"]["id"] == rec_id

    def test_latest_run_unknown_ticker(self, db_factory, settings_paper):
        with pytest.raises(HTTPException) as e:
            routes.latest_run("DOGECOIN", x_admin_auth_token=TOKEN)
        assert e.value.status_code == 404

    def test_run_cooldown_is_per_ticker(self, db_factory, settings_paper, monkeypatch):
        monkeypatch.setattr(routes, "_last_manual_run", {})
        bg = BackgroundTasks()
        r1 = routes.trigger_run(routes.RunRequest(commodity="GOLD"), bg,
                                x_admin_auth_token=TOKEN)
        assert r1["status"] == "started"
        # different ticker immediately: allowed
        r2 = routes.trigger_run(routes.RunRequest(commodity="NIFTY"), bg,
                                x_admin_auth_token=TOKEN)
        assert r2["status"] == "started"
        # same ticker immediately: blocked
        with pytest.raises(HTTPException) as e:
            routes.trigger_run(routes.RunRequest(commodity="GOLD"), bg,
                               x_admin_auth_token=TOKEN)
        assert e.value.status_code == 429


# ── IV-history endpoint + analytics in run payloads ──────────────────────────

class TestIvHistory:
    def _seed_runs(self, factory, n=3):
        base = datetime.now(IST) - timedelta(hours=n)
        with factory() as s:
            for i in range(n):
                s.add(AgentRun(
                    run_id=f"ivh-{i}", commodity="GOLD",
                    started_at=base + timedelta(hours=i), status="COMPLETED",
                    atm_iv=0.15 + i * 0.01,
                    regime_json=json.dumps({"realized_vol": 0.10}),
                    analytics_json=json.dumps(
                        {"iv_trend": {"direction": "expanding", "change_pct": 8.0,
                                      "samples": n}}),
                ))
            # a run with no IV must be excluded from the series
            s.add(AgentRun(run_id="ivh-null", commodity="GOLD",
                           started_at=base + timedelta(hours=n), status="FAILED"))
            s.commit()

    def test_series_ascending_with_vrp(self, db_factory, settings_paper):
        self._seed_runs(db_factory)
        out = routes.iv_history("GOLD", x_admin_auth_token=TOKEN)
        pts = out["points"]
        assert len(pts) == 3
        assert pts[0]["iv"] == 0.15                              # oldest → newest
        assert pts[-1]["iv"] == pytest.approx(0.17)
        assert pts[0]["rv"] == 0.10
        assert pts[0]["vrp"] == round(0.15 - 0.10, 4)
        assert out["iv_trend"]["direction"] == "expanding"

    def test_unknown_ticker_404(self, db_factory, settings_paper):
        with pytest.raises(HTTPException) as e:
            routes.iv_history("DOGECOIN", x_admin_auth_token=TOKEN)
        assert e.value.status_code == 404

    def test_latest_run_carries_analytics_stage(self, db_factory, settings_paper):
        now = datetime.now(IST)
        quant = {"vrp": {"vrp_pts": 4.2}, "stress": {"rows": []}}
        with db_factory() as s:
            s.add(AgentRun(run_id="r-an", commodity="NIFTY", started_at=now,
                           status="RUNNING", events_json="[]", strikes_json="[]",
                           regime_json="{}", analytics_json=json.dumps(quant)))
            s.commit()
        out = routes.latest_run("NIFTY", x_admin_auth_token=TOKEN)
        stages = {st["key"]: st["done"] for st in out["run"]["stages"]}
        assert stages["analytics"] and not stages["trend"]
        assert out["run"]["analytics"]["vrp"]["vrp_pts"] == 4.2

    def test_analyze_page_has_analytics_ui(self, settings_paper):
        from app.commodity_agents.dashboard import ANALYZE_HTML
        assert "iv-history" in ANALYZE_HTML          # chart endpoint wired
        assert "renderAnalytics" in ANALYZE_HTML
        assert "Vol analytics" in ANALYZE_HTML


# ── journal, calibration, portfolio greeks, briefing ─────────────────────────

from app.commodity_agents.storage import CommodityTradeJournal
from app.storage import ClosedTrade, Instrument, Order, Position


def _mk_order(s, alert_id, sym, txn="SELL", straddle_id=None, exch="MCX"):
    now = datetime.now(IST)
    o = Order(alert_id=alert_id, variety="regular", exchange=exch, tradingsymbol=sym,
              transaction_type=txn, order_type="MARKET", product="NRML", quantity=1,
              status="COMPLETE", placed_at=now, updated_at=now, dry_run=True,
              straddle_id=straddle_id)
    s.add(o)
    s.flush()
    return o


def _mk_position(s, order, underlying="GOLD", itype="CE", entry=800.0, exch="MCX"):
    now = datetime.now(IST)
    p = Position(order_id=order.id, exchange=exch, tradingsymbol=order.tradingsymbol,
                 underlying=underlying, instrument_type=itype, entry_premium=entry,
                 current_sl=entry * 1.5, quantity=1, lot_size=1,
                 opened_at=now, last_updated_at=now)
    s.add(p)
    s.flush()
    return p


class TestJournal:
    def test_approve_writes_paper_journal_row(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory)
        out, _ = _two_step(rec_id, "approve")
        assert out["status"] == "APPROVED"
        with db_factory() as s:
            row = s.query(CommodityTradeJournal).one()
            assert row.mode == "paper" and row.alert_id is None
            ctx = json.loads(row.entry_context_json)
            assert ctx["judge_confidence"] == 0.8

    def test_reject_writes_no_journal(self, db_factory, settings_paper):
        rec_id = _seed_rec(db_factory)
        _two_step(rec_id, "reject")
        with db_factory() as s:
            assert s.query(CommodityTradeJournal).count() == 0

    def test_sync_backfills_realized_pnl(self, db_factory, settings_paper):
        from app.commodity_agents.journal import sync_closed_trades
        now = datetime.now(IST)
        rec_id = _seed_rec(db_factory)
        with db_factory() as s:
            s.add(CommodityTradeJournal(
                recommendation_id=rec_id, commodity="GOLD", mode="live",
                alert_id=777, entered_at=now, entry_context_json="{}", lots=1))
            o1 = _mk_order(s, 777, "GOLD26JUL72000CE")
            o2 = _mk_order(s, 777, "GOLD26JUL72000PE")
            p1 = _mk_position(s, o1, itype="CE")
            p2 = _mk_position(s, o2, itype="PE")
            for pos, pnl in ((p1, 5000.0), (p2, -1200.0)):
                s.add(ClosedTrade(position_id=pos.id, exchange="MCX",
                                  tradingsymbol=pos.tradingsymbol, entry_premium=800,
                                  exit_premium=700, pnl=pnl, exit_reason="TARGET_HIT",
                                  opened_at=now, closed_at=now))
            s.commit()
        assert sync_closed_trades(db_factory) == 1
        with db_factory() as s:
            row = s.query(CommodityTradeJournal).one()
            assert row.realized_pnl == 3800.0
            assert row.exit_reason == "TARGET_HIT"

    def test_sync_waits_for_open_leg(self, db_factory, settings_paper):
        from app.commodity_agents.journal import sync_closed_trades
        now = datetime.now(IST)
        rec_id = _seed_rec(db_factory)
        with db_factory() as s:
            s.add(CommodityTradeJournal(
                recommendation_id=rec_id, commodity="GOLD", mode="live",
                alert_id=888, entered_at=now, entry_context_json="{}", lots=1))
            o1 = _mk_order(s, 888, "GOLD26JUL72000CE")
            _mk_position(s, o1)          # position open, no ClosedTrade yet
            s.commit()
        assert sync_closed_trades(db_factory) == 0

    def test_expectancy_by_regime(self, db_factory, settings_paper):
        from app.commodity_agents.journal import expectancy_stats
        now = datetime.now(IST)
        rec_id = _seed_rec(db_factory)
        with db_factory() as s:
            for regime, pnl in (("range-bound", 4000.0), ("range-bound", -1000.0),
                                ("trending", -2500.0)):
                s.add(CommodityTradeJournal(
                    recommendation_id=rec_id, commodity="GOLD", mode="live",
                    alert_id=None, entered_at=now,
                    entry_context_json=json.dumps({"regime": regime}),
                    lots=1, realized_pnl=pnl))
            s.commit()
            stats = expectancy_stats(s)
        assert stats["range-bound"]["trades"] == 2
        assert stats["range-bound"]["win_rate_pct"] == 50.0
        assert stats["trending"]["worst"] == -2500


class TestCalibration:
    def _seed_scored_setup(self, factory, commodity, strategy, direction,
                           implied_pct, ref_price, risk_flags=("low", "low", "low")):
        now = datetime.now(IST)
        with factory() as s:
            run = AgentRun(
                run_id=f"cal-{commodity}-{strategy}", commodity=commodity,
                started_at=now - timedelta(hours=30), status="COMPLETED",
                analytics_json=json.dumps({"expected_move": {
                    "underlying_price": ref_price, "implied_move_pct": implied_pct,
                    "days_to_expiry": 20.0}}),
                trend_json=json.dumps({"risk_flag": risk_flags[0]}),
                event_debate_json=json.dumps({"risk_flag": risk_flags[1]}),
                vol_json=json.dumps({"risk_flag": risk_flags[2]}),
            )
            s.add(run)
            s.commit()
            rec = CommodityRecommendation(
                run_pk=run.id, commodity=commodity,
                created_at=now - timedelta(hours=30),
                direction=direction, strategy_type=strategy,
                strikes_json="[]", confidence=0.8, reasoning_summary="t",
                dissent_json="[]", risk_vetoed=False, status="PROPOSED")
            s.add(rec)
            s.commit()
            return rec.id

    def _mk_fut(self, s, underlying, token):
        s.add(Instrument(
            instrument_token=token, exchange_token=token,
            tradingsymbol=f"{underlying}26AUGFUT", name=underlying,
            expiry=datetime.now(IST).date() + timedelta(days=40), strike=None,
            tick_size=1.0, lot_size=1, instrument_type="FUT",
            segment="MCX", exchange="MCX"))

    def test_win_and_avoided_outcomes(self, db_factory, settings_paper):
        from app.commodity_agents.calibration import calibration_stats, evaluate_pending
        # GOLD short straddle, tiny realized move -> WIN
        self._seed_scored_setup(db_factory, "GOLD", "short_straddle", "SELL",
                                implied_pct=2.0, ref_price=72000.0)
        # SILVER NO_TRADE, huge realized move -> AVOIDED
        self._seed_scored_setup(db_factory, "SILVER", "none", "NO_TRADE",
                                implied_pct=2.0, ref_price=90000.0,
                                risk_flags=("high", "high", "high"))
        with db_factory() as s:
            self._mk_fut(s, "GOLD", 9001)
            self._mk_fut(s, "SILVER", 9002)
            s.commit()
        kite = MagicMock()
        prices = {"MCX:GOLD26AUGFUT": 72100.0, "MCX:SILVER26AUGFUT": 97000.0}
        kite.ltp.side_effect = lambda keys: {keys[0]: {"last_price": prices[keys[0]]}}
        assert evaluate_pending(db_factory, kite) == 2
        with db_factory() as s:
            outcomes = {r.commodity: r.outcome
                        for r in s.query(CommodityRecommendation).all()}
            assert outcomes["GOLD"] == "WIN"
            assert outcomes["SILVER"] == "AVOIDED"
            stats = calibration_stats(s)
        assert stats["judge"]["actionable_recs"] == 1
        assert stats["judge"]["win_rate_pct"] == 100.0
        assert stats["no_trade"]["avoided"] == 1
        # high flags saw danger, low flags did not
        assert stats["risk_flag_reliability"]["vol"]["high"]["danger_rate_pct"] == 100.0
        assert stats["risk_flag_reliability"]["vol"]["low"]["danger_rate_pct"] == 0.0

    def test_missing_quant_marks_unscored(self, db_factory, settings_paper):
        from app.commodity_agents.calibration import evaluate_pending
        now = datetime.now(IST)
        with db_factory() as s:
            run = AgentRun(run_id="cal-noq", commodity="GOLD",
                           started_at=now - timedelta(hours=30), status="COMPLETED")
            s.add(run)
            s.commit()
            s.add(CommodityRecommendation(
                run_pk=run.id, commodity="GOLD", created_at=now - timedelta(hours=30),
                direction="SELL", strategy_type="short_straddle", strikes_json="[]",
                confidence=0.8, reasoning_summary="t", dissent_json="[]",
                risk_vetoed=False, status="PROPOSED"))
            s.commit()
        assert evaluate_pending(db_factory, MagicMock()) == 0
        with db_factory() as s:
            assert s.query(CommodityRecommendation).one().outcome == "UNSCORED"


class TestPortfolioGreeks:
    def test_short_straddle_greeks_and_grouping(self, db_factory, settings_paper):
        from py_vollib.black import black as b76_price
        from app.commodity_agents.portfolio import compute_portfolio_greeks
        now = datetime.now(IST)
        expiry = now.date() + timedelta(days=21)
        with db_factory() as s:
            s.add(Instrument(instrument_token=9100, exchange_token=9100,
                             tradingsymbol="GOLD26JULFUT", name="GOLD",
                             expiry=now.date() + timedelta(days=24), strike=None,
                             tick_size=1.0, lot_size=1, instrument_type="FUT",
                             segment="MCX", exchange="MCX"))
            for i, (sym, itype) in enumerate((("GOLD26JUL72000CE", "CE"),
                                              ("GOLD26JUL72000PE", "PE"))):
                s.add(Instrument(instrument_token=9101 + i, exchange_token=9101 + i,
                                 tradingsymbol=sym, name="GOLD", expiry=expiry,
                                 strike=72000.0, tick_size=0.5, lot_size=1,
                                 instrument_type=itype, segment="MCX", exchange="MCX"))
            o1 = _mk_order(s, 1, "GOLD26JUL72000CE", straddle_id="st-1")
            o2 = _mk_order(s, 1, "GOLD26JUL72000PE", straddle_id="st-1")
            _mk_position(s, o1, itype="CE")
            _mk_position(s, o2, itype="PE")
            s.commit()

        t = 21 / 365.25
        kite = MagicMock()
        kite.quote.return_value = {
            "MCX:GOLD26JUL72000CE": {"last_price": round(
                b76_price("c", 72000.0, 72000.0, t, 0.07, 0.15), 2)},
            "MCX:GOLD26JUL72000PE": {"last_price": round(
                b76_price("p", 72000.0, 72000.0, t, 0.07, 0.15), 2)},
        }
        kite.ltp.return_value = {"MCX:GOLD26JULFUT": {"last_price": 72000.0}}
        with db_factory() as s:
            book = compute_portfolio_greeks(s, kite, settings_paper, now=now)
        assert len(book["positions"]) == 2
        deltas = [p["delta"] for p in book["positions"]]
        assert all(d is not None for d in deltas)
        # short CE -> negative delta, short PE -> positive delta
        assert min(deltas) < 0 < max(deltas)
        st = book["straddles"]["st-1"]
        assert abs(st["net_delta_per_lot"]) < 0.15       # ATM straddle ~ neutral
        assert book["totals"]["net_theta_per_day"] > 0   # short premium collects theta


class TestBriefing:
    def test_briefing_contains_gap_vrp_events(self, db_factory, settings_paper):
        from app.commodity_agents.briefing import build_briefing
        now = datetime.now(IST)
        settings = settings_paper.model_copy(
            update={"COMMODITY_AGENTS_COMMODITIES": ["GOLD"]})
        with db_factory() as s:
            s.add(Instrument(instrument_token=9200, exchange_token=9200,
                             tradingsymbol="GOLD26JULFUT", name="GOLD",
                             expiry=now.date() + timedelta(days=24), strike=None,
                             tick_size=1.0, lot_size=1, instrument_type="FUT",
                             segment="MCX", exchange="MCX"))
            s.add(AgentRun(
                run_id="brief-1", commodity="GOLD", started_at=now,
                status="COMPLETED",
                regime_json=json.dumps({"label": "range-bound"}),
                analytics_json=json.dumps({
                    "vrp": {"vrp_pts": 6.4},
                    "iv_trend": {"direction": "contracting"}})))
            s.commit()
            kite = MagicMock()
            kite.quote.return_value = {"MCX:GOLD26JULFUT": {
                "last_price": 72720.0, "ohlc": {"close": 72000.0}}}
            text = build_briefing(s, kite, settings, now=now)
        assert "GOLD" in text
        assert "+1.00%" in text
        assert "VRP +6.4" in text
        assert "range-bound" in text


class TestDeskPage:
    def test_desk_page_served(self, settings_paper):
        html = routes.desk_page()
        assert "Desk" in html
        assert "/portfolio-greeks" in html
        assert "/journal" in html
        assert "/calibration" in html
        assert "X-Admin-Auth-Token" not in html      # cookie-session auth only

    def test_nav_links_wired(self, settings_paper):
        from app.commodity_agents.dashboard import ANALYZE_HTML, DASHBOARD_HTML
        assert "/commodity-agents/desk" in DASHBOARD_HTML
        assert "/commodity-agents/desk" in ANALYZE_HTML


# ── cookie-session auth (no credential in the browser) ──────────────────────

class TestSessionAuth:
    def _mk_session(self, factory, hours=1) -> str:
        import secrets as _secrets
        from datetime import timezone as _tz
        from app.storage import WebSession
        token = _secrets.token_hex(32)
        with factory() as s:
            s.add(WebSession(token=token,
                             created_at=datetime.now(_tz.utc),
                             expires_at=datetime.now(_tz.utc) + timedelta(hours=hours)))
            s.commit()
        return token

    def test_valid_session_cookie_authorizes(self, db_factory, settings_paper):
        token = self._mk_session(db_factory)
        out = routes.latest_recommendations(x_admin_auth_token=None, zb_session=token)
        assert "recommendations" in out

    def test_expired_session_rejected(self, db_factory, settings_paper):
        token = self._mk_session(db_factory, hours=-1)
        with pytest.raises(HTTPException) as e:
            routes.latest_recommendations(x_admin_auth_token=None, zb_session=token)
        assert e.value.status_code == 401

    def test_header_token_still_works_for_api(self, db_factory, settings_paper):
        out = routes.latest_recommendations(x_admin_auth_token=TOKEN, zb_session=None)
        assert "recommendations" in out

    def test_pages_redirect_to_login_without_session(self, db_factory, settings_paper):
        from fastapi.responses import RedirectResponse
        for page, path in ((routes.dashboard_page, "dashboard"),
                           (routes.analyze_page, "analyze"),
                           (routes.desk_page, "desk")):
            resp = page(zb_session=None)
            assert isinstance(resp, RedirectResponse)
            assert f"/login?next=/commodity-agents/{path}" in resp.headers["location"]

    def test_pages_served_with_valid_session(self, db_factory, settings_paper):
        token = self._mk_session(db_factory)
        html = routes.dashboard_page(zb_session=token)
        assert isinstance(html, str) and "Commodity Agents" in html

    def test_no_localstorage_credential_in_any_page(self, settings_paper):
        from app.commodity_agents.dashboard import ANALYZE_HTML, DASHBOARD_HTML, DESK_HTML
        for html in (DASHBOARD_HTML, ANALYZE_HTML, DESK_HTML):
            assert "setItem('ca_token'" not in html
            assert "X-Admin-Auth-Token" not in html
            assert "/login?next=" in html


# ── wave 4: sizing, regime flip, slippage/MAE, weekly report ─────────────────

class TestSuggestedLots:
    def test_budget_and_margin_clamps(self, settings_paper):
        from app.commodity_agents.orchestrator import _suggested_lots
        s = settings_paper.model_copy(update={
            "MAX_DAILY_LOSS_ABS": 30000.0, "COMMODITY_AGENT_MAX_LOTS": 5,
            "COMMODITY_MAX_MARGIN_UTIL_PCT": 60.0})
        # budget allows 3 lots (30000/9000), margin allows 2 (0.6*400000/110000)
        assert _suggested_lots(s, 9000.0, 110000.0, 400000.0) == 2
        # no margin info -> budget only
        assert _suggested_lots(s, 9000.0, None, None) == 3
        # worst case above budget -> honest zero
        assert _suggested_lots(s, 50000.0, None, None) == 0
        # cap by max lots
        assert _suggested_lots(s, 1000.0, None, None) == 5
        assert _suggested_lots(s, None, None, None) is None

    def test_suggested_lots_persisted_and_served(self, db_factory, settings_paper):
        now = datetime.now(IST)
        with db_factory() as s:
            run = AgentRun(run_id="sl-1", commodity="GOLD", started_at=now,
                           status="COMPLETED")
            s.add(run)
            s.commit()
            s.add(CommodityRecommendation(
                run_pk=run.id, commodity="GOLD", created_at=now, direction="SELL",
                strategy_type="short_straddle", strikes_json="[]", confidence=0.8,
                reasoning_summary="t", dissent_json="[]", risk_vetoed=False,
                status="PROPOSED", suggested_lots=3))
            s.commit()
        out = routes.latest_recommendations(x_admin_auth_token=TOKEN)
        assert out["recommendations"]["GOLD"]["suggested_lots"] == 3

    def test_ui_prefills_lots(self):
        from app.commodity_agents.dashboard import ANALYZE_HTML, DASHBOARD_HTML
        for html in (ANALYZE_HTML, DASHBOARD_HTML):
            assert "recLots" in html
            assert "suggested_lots" in html


class TestRegimeFlipAlert:
    def _seed_prev_run_and_short(self, factory, label="range-bound"):
        now = datetime.now(IST)
        with factory() as s:
            s.add(AgentRun(run_id="rf-prev", commodity="GOLD",
                           started_at=now - timedelta(minutes=30), status="COMPLETED",
                           regime_json=json.dumps({"label": label})))
            o = _mk_order(s, 1, "GOLD26JUL72000CE")
            _mk_position(s, o, itype="CE")
            s.commit()

    def test_flip_with_open_shorts_alerts(self, db_factory, settings_paper):
        from app.commodity_agents.orchestrator import _regime_flip_alert
        self._seed_prev_run_and_short(db_factory)
        with db_factory() as s:
            cur = AgentRun(run_id="rf-cur", commodity="GOLD",
                           started_at=datetime.now(IST), status="RUNNING",
                           regime_json=json.dumps({"label": "trending"}))
            s.add(cur)
            s.commit()
            with patch("app.commodity_agents.notify.send_telegram") as tg:
                _regime_flip_alert(s, settings_paper, cur, "GOLD", "trending")
                assert tg.called
                assert "REGIME FLIP" in tg.call_args[0][1]

    def test_no_alert_without_positions(self, db_factory, settings_paper):
        from app.commodity_agents.orchestrator import _regime_flip_alert
        now = datetime.now(IST)
        with db_factory() as s:
            s.add(AgentRun(run_id="rf-p2", commodity="SILVER",
                           started_at=now - timedelta(minutes=30), status="COMPLETED",
                           regime_json=json.dumps({"label": "range-bound"})))
            cur = AgentRun(run_id="rf-c2", commodity="SILVER", started_at=now,
                           status="RUNNING")
            s.add(cur)
            s.commit()
            with patch("app.commodity_agents.notify.send_telegram") as tg:
                _regime_flip_alert(s, settings_paper, cur, "SILVER", "trending")
                assert not tg.called

    def test_no_alert_on_benign_label(self, db_factory, settings_paper):
        from app.commodity_agents.orchestrator import _regime_flip_alert
        with db_factory() as s:
            cur = AgentRun(run_id="rf-c3", commodity="GOLD",
                           started_at=datetime.now(IST), status="RUNNING")
            s.add(cur)
            s.commit()
            with patch("app.commodity_agents.notify.send_telegram") as tg:
                _regime_flip_alert(s, settings_paper, cur, "GOLD", "range-bound")
                assert not tg.called


class TestSlippageAndExcursions:
    def test_sync_computes_slippage_and_mae(self, db_factory, settings_paper):
        from app.commodity_agents.journal import sync_closed_trades
        now = datetime.now(IST)
        with db_factory() as s:
            run = AgentRun(
                run_id="slip-run", commodity="GOLD", started_at=now,
                status="COMPLETED",
                strikes_json=json.dumps([
                    {"tradingsymbol": "GOLD26JUL72000CE", "ltp": 800.0},
                    {"tradingsymbol": "GOLD26JUL72000PE", "ltp": 800.0}]))
            s.add(run)
            s.commit()
            rec = CommodityRecommendation(
                run_pk=run.id, commodity="GOLD", created_at=now, direction="SELL",
                strategy_type="short_straddle", strikes_json="[]", confidence=0.8,
                reasoning_summary="t", dissent_json="[]", risk_vetoed=False,
                status="APPROVED")
            s.add(rec)
            s.commit()
            s.add(CommodityTradeJournal(
                recommendation_id=rec.id, commodity="GOLD", mode="live",
                alert_id=555, entered_at=now, entry_context_json="{}", lots=1))
            for sym, itype, fill, worst, best in (
                    ("GOLD26JUL72000CE", "CE", 792.0, 1050.0, 700.0),
                    ("GOLD26JUL72000PE", "PE", 796.0, 900.0, 500.0)):
                o = _mk_order(s, 555, sym)
                o.fill_price = fill
                p = _mk_position(s, o, itype=itype, entry=fill)
                p.max_adverse_price = worst
                p.max_favorable_price = best
                s.add(ClosedTrade(position_id=p.id, exchange="MCX",
                                  tradingsymbol=sym, entry_premium=fill,
                                  exit_premium=fill * 0.5, pnl=3000.0,
                                  exit_reason="TARGET_HIT", opened_at=now,
                                  closed_at=now))
            s.commit()
        assert sync_closed_trades(db_factory) == 1
        with db_factory() as s:
            row = s.query(CommodityTradeJournal).one()
            # SELL fills below quote: (792-800)/800 and (796-800)/800, weighted -> -0.75%
            assert row.slippage_pct == pytest.approx(-0.75, abs=0.01)
            # CE leg: worst 1050 vs entry 792 -> +32.6% MAE (worst leg)
            assert row.mae_pct == pytest.approx(32.6, abs=0.2)
            # PE leg best 500 vs entry 796 -> 37.2% MFE
            assert row.mfe_pct == pytest.approx(37.2, abs=0.2)

    def test_trailing_tracks_excursions(self):
        from unittest.mock import MagicMock as MM
        from app.trailing import TrailingSlManager
        mgr = TrailingSlManager(session_factory=MM(), kite_fetcher=MM())
        mgr.register(tradingsymbol="X", instrument_token=1, exchange="MCX",
                     entry_side="SELL", sl_pct=0.5, qty=1, product="NRML",
                     target_price=400.0, initial_sl=1200.0, fill_price=800.0,
                     gtt_db_id=1, kite_gtt_id=None, tick_size=0.05)
        pos = mgr._positions[1]
        mgr.on_ticks([{"instrument_token": 1, "last_price": 950.0}])   # adverse
        mgr.on_ticks([{"instrument_token": 1, "last_price": 700.0}])   # favorable
        mgr.on_ticks([{"instrument_token": 1, "last_price": 900.0}])   # neither extreme
        assert pos.worst_price == 950.0
        assert pos.best_price == 700.0
        assert pos.excursions_dirty


class TestWeeklyReport:
    def test_report_composes_sections(self, db_factory, settings_paper):
        from app.commodity_agents.weekly_report import build_weekly_report
        now = datetime.now(IST)
        settings = settings_paper.model_copy(
            update={"COMMODITY_AGENTS_COMMODITIES": ["GOLD"]})
        with db_factory() as s:
            run = AgentRun(run_id="wr-1", commodity="GOLD", started_at=now,
                           status="COMPLETED",
                           regime_json=json.dumps({"label": "range-bound",
                                                   "atm_iv": 0.32,
                                                   "iv_percentile": 74.0}))
            s.add(run)
            s.commit()
            rec = CommodityRecommendation(
                run_pk=run.id, commodity="GOLD", created_at=now, direction="SELL",
                strategy_type="short_straddle", strikes_json="[]", confidence=0.8,
                reasoning_summary="t", dissent_json="[]", risk_vetoed=False,
                status="APPROVED")
            s.add(rec)
            s.commit()
            s.add(CommodityTradeJournal(
                recommendation_id=rec.id, commodity="GOLD", mode="live",
                alert_id=None, entered_at=now - timedelta(days=2),
                entry_context_json=json.dumps({"regime": "range-bound"}),
                lots=1, realized_pnl=5200.0, slippage_pct=-0.4, mae_pct=28.0))
            s.commit()
            text = build_weekly_report(s, settings, now=now)
        assert "Weekly desk review" in text
        assert "range-bound" in text
        assert "slippage" in text.lower()
        assert "pctile 74" in text
