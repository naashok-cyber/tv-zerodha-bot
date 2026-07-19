"""Tests for P2-a: app/state.py, app/scheduler.py, and session-gate wiring."""
from __future__ import annotations

import threading
from datetime import datetime, date
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.state as state
from app import scheduler as sched_module
from app.config import IST, UTC, Settings
from app.main import app, get_current_settings, get_db_session
from app.scheduler import daily_session_check, get_last_checked_at, make_scheduler
from app.storage import Alert, Base, Instrument


# ── Helpers ───────────────────────────────────────────────────────────────────

_TV_IP = "52.89.214.238"
_SECRET = "test-secret"


def _s(**kwargs) -> Settings:
    defaults = dict(
        _env_file=None,
        DRY_RUN=True,
        CAPITAL_PER_TRADE=10_000.0,
        RISK_PER_TRADE_PCT=100.0,
        RISK_PCT=Decimal("1.0"),
        MAX_DAILY_LOSS_ABS=100_000.0,
        MAX_DAILY_LOSS=Decimal("100000"),
        MAX_DAILY_LOSS_PCT=100.0,
        TOTAL_CAPITAL=100_000.0,
        FUTURES_SL_PCT=0.005,
        SL_PERCENT=Decimal("0.005"),
        SL_PREMIUM_PCT=0.30,
        BREAKEVEN_RR=1.0,
        TRAIL_RR=1.5,
        TRAIL_DISTANCE_RR=0.5,
        RR_RATIO=2.0,
        TARGET_DELTA=0.65,
        MAX_TRADES_PER_DAY=10,
        MAX_OPEN_POSITIONS=3,
        CONSECUTIVE_LOSSES_LIMIT=3,
        KITE_API_KEY="testkey",
        PRODUCT_TYPE="NRML",
        WEBHOOK_SECRET=_SECRET,
        TV_ALLOWED_IPS=[_TV_IP],
        DATABASE_URL="sqlite:///:memory:",
        DASHBOARD_PASSWORD="",
        SECRET_KEY="",
        PBKDF2_ITERATIONS=1,
        # Alert timestamp is 10:00 UTC = 15:30 IST; window must cover it.
        ENTRY_WINDOW_START="09:00",
        ENTRY_WINDOW_END="16:00",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _make_factory(engine=None):
    e = engine or _make_engine()
    return sessionmaker(bind=e, expire_on_commit=False)


def _make_alert(action: str = "BUY", symbol: str = "NIFTY") -> AlertPayload:
    return AlertPayload(
        symbol=symbol,
        action=action,
        price=Decimal("19500"),
        premium=None,
        timeframe="5",
        alert_id="sched_test_001",
        timestamp=datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC),
    )


def _seed_instrument(session, symbol: str = "NIFTY") -> None:
    session.add(Instrument(
        tradingsymbol=f"{symbol}26APR26CE19500",
        name=symbol,
        exchange="NFO",
        instrument_type="CE",
        segment="NFO-OPT",
        expiry=date(2026, 4, 26),
        strike=19500.0,
        lot_size=50,
        tick_size=0.05,
    ))
    session.commit()


# ── app/state.py ──────────────────────────────────────────────────────────────

class TestSessionInvalidFlag:
    def test_default_false(self):
        assert state.get_session_invalid() is False

    def test_set_true_then_false(self):
        state.set_session_invalid(True)
        assert state.get_session_invalid() is True
        state.set_session_invalid(False)
        assert state.get_session_invalid() is False

    def test_thread_safe_concurrent_writes(self):
        """Multiple threads toggling the flag must not corrupt the value."""
        errors = []

        def toggle(n: int):
            try:
                for _ in range(n):
                    state.set_session_invalid(True)
                    state.set_session_invalid(False)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=toggle, args=(500,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert state.get_session_invalid() is False


# ── app/scheduler.py ─────────────────────────────────────────────────────────

class TestDailySessionCheck:
    def setup_method(self):
        sched_module._last_checked_at = None

    def test_valid_token_clears_flag(self):
        state.set_session_invalid(True)
        mock_kite = MagicMock()
        mock_kite.profile.return_value = {"user_id": "XY1234"}
        mock_sm = MagicMock()
        mock_sm.get_kite.return_value = mock_kite

        with patch("app.scheduler.get_session_manager", return_value=mock_sm):
            daily_session_check(now=datetime(2026, 4, 27, 8, 0, tzinfo=IST))

        assert state.get_session_invalid() is False

    def test_invalid_token_sets_flag(self):
        state.set_session_invalid(False)
        mock_kite = MagicMock()
        mock_kite.profile.side_effect = Exception("Invalid token")
        mock_sm = MagicMock()
        mock_sm.get_kite.return_value = mock_kite

        with patch("app.scheduler.get_session_manager", return_value=mock_sm):
            daily_session_check(now=datetime(2026, 4, 27, 8, 0, tzinfo=IST))

        assert state.get_session_invalid() is True

    def test_updates_last_checked_at(self):
        now = datetime(2026, 4, 27, 8, 0, tzinfo=IST)
        mock_kite = MagicMock()
        mock_sm = MagicMock()
        mock_sm.get_kite.return_value = mock_kite

        with patch("app.scheduler.get_session_manager", return_value=mock_sm):
            daily_session_check(now=now)

        assert get_last_checked_at() == now

    def test_get_kite_raises_sets_flag(self):
        """get_session_manager().get_kite() itself throwing must set the flag."""
        state.set_session_invalid(False)
        mock_sm = MagicMock()
        mock_sm.get_kite.side_effect = RuntimeError("no token file")

        with patch("app.scheduler.get_session_manager", return_value=mock_sm):
            daily_session_check(now=datetime(2026, 4, 27, 8, 0, tzinfo=IST))

        assert state.get_session_invalid() is True


# ── /auth/status endpoint ─────────────────────────────────────────────────────

class TestAuthStatusEndpoint:
    def _setup(self, token_info: dict, settings: Settings | None = None):
        s = settings or _s()
        factory = _make_factory()
        app.dependency_overrides[get_current_settings] = lambda: s
        app.dependency_overrides[get_db_session] = lambda: factory()
        mock_mgr = MagicMock()
        mock_mgr.get_token_info.return_value = token_info
        return mock_mgr

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_returns_valid_when_token_fresh(self):
        sched_module._last_checked_at = None
        mock_mgr = self._setup({"is_valid": True, "age_hours": 1.5, "reason": None})

        with patch("app.main.get_session_manager", return_value=mock_mgr):
            resp = TestClient(app, raise_server_exceptions=True).get("/auth/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_valid"] is True
        assert body["token_age_hours"] == 1.5
        assert body["reason"] is None
        assert body["checked_at"] is None

    def test_returns_invalid_when_no_token_file(self):
        mock_mgr = self._setup({"is_valid": False, "age_hours": None, "reason": "no token file"})

        with patch("app.main.get_session_manager", return_value=mock_mgr):
            resp = TestClient(app, raise_server_exceptions=True).get("/auth/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_valid"] is False
        assert body["reason"] == "no token file"
        assert body["token_age_hours"] is None

    def test_returns_invalid_when_token_stale(self):
        mock_mgr = self._setup({
            "is_valid": False,
            "age_hours": 21.0,
            "reason": "token is 21.0h old (limit 20h)",
        })

        with patch("app.main.get_session_manager", return_value=mock_mgr):
            resp = TestClient(app, raise_server_exceptions=True).get("/auth/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_valid"] is False
        assert body["token_age_hours"] == 21.0

    def test_checked_at_populated_after_job_run(self):
        now = datetime(2026, 4, 27, 8, 0, tzinfo=IST)
        sched_module._last_checked_at = now
        mock_mgr = self._setup({"is_valid": True, "age_hours": 2.0, "reason": None})

        with patch("app.main.get_session_manager", return_value=mock_mgr):
            resp = TestClient(app, raise_server_exceptions=True).get("/auth/status")

        assert resp.json()["checked_at"] == now.isoformat()

    def test_dry_run_reflected(self):
        mock_mgr = self._setup(
            {"is_valid": True, "age_hours": 1.0, "reason": None},
            settings=_s(DRY_RUN=True),
        )

        with patch("app.main.get_session_manager", return_value=mock_mgr):
            resp = TestClient(app, raise_server_exceptions=True).get("/auth/status")

        assert resp.json()["dry_run"] is True


# ── SESSION_INVALID gate in _process_alert ────────────────────────────────────

import secrets as _secrets

import app.main as main_module
from app.expiry_resolver import ResolvedExpiry
from app.main import _process_alert
from app.schemas import AlertPayload


def _seed_alert_row(session, tv_ticker: str = "NIFTY", suffix: str = "") -> Alert:
    row = Alert(
        received_at=datetime(2026, 4, 27, 10, 0, tzinfo=IST),
        strategy_id=f"gate_test{suffix}",
        tv_ticker=tv_ticker,
        tv_exchange="NSE",
        action="BUY",
        product="NRML",
        processed=False,
        idempotency_key=f"gate_{suffix}_{_secrets.token_hex(4)}",
        raw_payload="{}",
    )
    session.add(row)
    session.flush()
    return row


_RESOLVED = ResolvedExpiry(expiry_date=date(2026, 4, 26), days_to_expiry=0, rule_used="NEAREST_WEEKLY")


class TestSessionInvalidGate:
    def _fresh_token_mgr(self):
        mock_mgr = MagicMock()
        mock_mgr.get_token_info.return_value = {"is_valid": True, "age_hours": 1.0, "reason": None}
        return mock_mgr

    def test_invalid_session_flag_blocks_live_order(self):
        """SESSION_INVALID=True + fresh token + DRY_RUN=False → blocked; place_entry not called."""
        state.set_session_invalid(True)
        factory = _make_factory()
        s = _s(DRY_RUN=False)

        with factory() as session:
            alert = _seed_alert_row(session, suffix="blk")
            alert_id = alert.id
            session.commit()

        with (
            patch("app.main.resolve_expiry", return_value=_RESOLVED),
            patch("app.main.place_entry") as mock_pe,
            patch("app.risk.check_risk_gates"),
            patch("app.main.get_session_manager", return_value=self._fresh_token_mgr()),
        ):
            main_module._SessionFactory = factory
            _process_alert(alert_id, _make_alert(), s)

        mock_pe.assert_not_called()

    def test_stale_token_blocks_live_order_even_when_flag_clear(self):
        """Expired token + SESSION_INVALID=False + DRY_RUN=False → blocked; this was the production bug."""
        state.set_session_invalid(False)
        factory = _make_factory()
        s = _s(DRY_RUN=False)
        mock_mgr = MagicMock()
        mock_mgr.get_token_info.return_value = {
            "is_valid": False, "age_hours": 24.6,
            "reason": "token is 24.6h old (limit 20h)",
        }

        with factory() as session:
            alert = _seed_alert_row(session, suffix="stale")
            alert_id = alert.id
            session.commit()

        with (
            patch("app.main.resolve_expiry", return_value=_RESOLVED),
            patch("app.main.place_entry") as mock_pe,
            patch("app.risk.check_risk_gates"),
            patch("app.main.get_session_manager", return_value=mock_mgr),
        ):
            main_module._SessionFactory = factory
            _process_alert(alert_id, _make_alert(), s)

        mock_pe.assert_not_called()
        assert state.get_session_invalid() is True  # gate syncs the flag for future alerts

    def test_dry_run_bypasses_session_gate(self):
        """SESSION_INVALID=True + DRY_RUN=True → gate skipped; DRY_RUN Order row is created."""
        state.set_session_invalid(True)
        factory = _make_factory()
        s = _s(DRY_RUN=True)

        with factory() as session:
            alert = _seed_alert_row(session, suffix="dry")
            alert_id = alert.id
            session.commit()

        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert(), s)

        # The DRY_RUN non-NG BUY path creates an Order(dry_run=True, quantity=0).
        # The SESSION_INVALID blocked path returns before creating any Order.
        from app.storage import Order
        with factory() as session:
            order = session.query(Order).filter_by(alert_id=alert_id).first()
        assert order is not None
        assert order.dry_run is True


# ── pnl_snapshot_job ──────────────────────────────────────────────────────────

class TestPnlSnapshotJob:
    def test_records_realized_with_null_mtm_when_no_kite(self):
        """Snapshot rows are written even without a Kite session: realised P&L
        from today's ClosedTrade rows, open_mtm null."""
        from datetime import timedelta
        from app.scheduler import pnl_snapshot_job
        from app.storage import ClosedTrade, PnlSnapshot, Position, Order, Alert

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        now = datetime.now(IST)
        with factory() as session:
            alert = Alert(
                received_at=now, strategy_id="t", tv_ticker="NIFTY",
                tv_exchange="NSE", action="BUY", product="NRML",
                idempotency_key="snap-test-1", raw_payload="{}",
            )
            session.add(alert)
            session.flush()
            order = Order(
                alert_id=alert.id, variety="regular", exchange="NFO",
                tradingsymbol="NIFTY25JUL24800PE", transaction_type="SELL",
                order_type="MARKET", product="NRML", quantity=75,
                status="COMPLETE", placed_at=now, updated_at=now,
            )
            session.add(order)
            session.flush()
            pos = Position(
                order_id=order.id, exchange="NFO",
                tradingsymbol="NIFTY25JUL24800PE", underlying="NIFTY",
                instrument_type="PE", quantity=75, entry_premium=100.0,
                current_sl=115.0, lot_size=75, opened_at=now,
                last_updated_at=now,
            )
            session.add(pos)
            session.flush()
            # dry_run=True matches the paper mode the default test settings
            # run in — the snapshot job samples the active mode's trades.
            session.add(ClosedTrade(
                position_id=pos.id, exchange="NFO",
                tradingsymbol="NIFTY25JUL24800PE", entry_premium=100.0,
                exit_premium=80.0, pnl=1500.0, exit_reason="TARGET_HIT",
                opened_at=now - timedelta(hours=2), closed_at=now,
                dry_run=True,
            ))
            session.commit()

        with patch("app.scheduler.get_session_manager",
                   side_effect=RuntimeError("no kite")):
            pnl_snapshot_job(factory, _s())

        with factory() as session:
            rows = session.query(PnlSnapshot).all()
        assert len(rows) == 1
        assert rows[0].realized == 1500.0
        assert rows[0].open_mtm is None
