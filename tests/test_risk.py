"""Tests for app/risk.py — pure unit tests; all DB operations use in-memory SQLite."""
from __future__ import annotations

import secrets as _secrets
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.config import IST, Settings
from app.main import (
    _on_gtt_filled,
    _process_alert,
    app,
    get_current_settings,
    get_db_session,
)
from app.risk import (
    RiskHaltError,
    check_risk_gates,
    compute_futures_qty,
    compute_option_qty,
    daily_loss_remaining,
    record_trade_result,
)
from app.storage import Alert, Base, ClosedTrade, Gtt, Instrument, Order, Position
from app.watcher import GttFilledEvent


# ── Shared DB helpers ─────────────────────────────────────────────────────────

def _make_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _now() -> datetime:
    return datetime.now(IST)


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        DRY_RUN=True,
        TOTAL_CAPITAL=100_000.0,
        MAX_DAILY_LOSS_PCT=2.0,
        MAX_DAILY_LOSS=Decimal("2000"),
        RISK_PCT=Decimal("0.01"),
        CAPITAL_PER_TRADE=10_000.0,
        DATABASE_URL="sqlite:///:memory:",
        KITE_API_KEY="testkey",
        WEBHOOK_SECRET="sec",
        SECRET_KEY="",
        PBKDF2_ITERATIONS=1,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _seed_entry(session, tradingsymbol: str = "NIFTY_TEST", kite_order_id: str | None = None) -> tuple[Order, Position]:
    """Seed Alert → Order → Position for record_trade_result tests."""
    now = _now()
    oid = kite_order_id or f"ORD_{_secrets.token_hex(4)}"
    alert = Alert(
        received_at=now,
        strategy_id="test",
        tv_ticker="NIFTY",
        tv_exchange="NSE",
        action="BUY",
        product="NRML",
        idempotency_key=f"ik_{_secrets.token_hex(6)}",
        raw_payload="{}",
        processed=True,
    )
    session.add(alert)
    session.flush()
    order = Order(
        alert_id=alert.id,
        kite_order_id=oid,
        variety="regular",
        exchange="NFO",
        tradingsymbol=tradingsymbol,
        transaction_type="BUY",
        order_type="MARKET",
        product="NRML",
        quantity=50,
        status="COMPLETE",
        placed_at=now,
        updated_at=now,
        dry_run=False,
    )
    session.add(order)
    session.flush()
    pos = Position(
        order_id=order.id,
        exchange="NFO",
        tradingsymbol=tradingsymbol,
        underlying="NIFTY",
        instrument_type="CE",
        entry_premium=100.0,
        current_sl=70.0,
        quantity=50,
        lot_size=50,
        opened_at=now,
        last_updated_at=now,
    )
    session.add(pos)
    session.flush()
    return order, pos


def _seed_closed_trade(session, pnl: float, closed_at: datetime | None = None) -> ClosedTrade:
    """Seed a complete trade chain and return the ClosedTrade row."""
    now = closed_at or _now()
    order, pos = _seed_entry(session)
    ct = ClosedTrade(
        position_id=pos.id,
        exchange=pos.exchange,
        tradingsymbol=pos.tradingsymbol,
        entry_premium=pos.entry_premium,
        exit_premium=70.0 if pnl < 0 else 130.0,
        pnl=pnl,
        exit_reason="SL_HIT" if pnl < 0 else "TARGET_HIT",
        opened_at=pos.opened_at,
        closed_at=now,
    )
    session.add(ct)
    session.flush()
    return ct


# ── compute_option_qty ────────────────────────────────────────────────────────

def test_compute_option_qty_normal():
    """floor(10000/50/50)*50 = 4*50 = 200."""
    assert compute_option_qty(Decimal("10000"), Decimal("50"), Decimal("50")) == 200


def test_compute_option_qty_sub_lot_returns_0():
    """floor(1000/100/100)*100 = 0 → returns 0 (less than 1 lot)."""
    assert compute_option_qty(Decimal("1000"), Decimal("100"), Decimal("100")) == 0


def test_compute_option_qty_decimal_precision():
    """floor(10000/33.33/50)*50 — Decimal arithmetic avoids float truncation errors."""
    # 10000/33.33 = 300.030..., /50 = 6.000..., floor=6, *50=300
    qty = compute_option_qty(Decimal("10000"), Decimal("33.33"), Decimal("50"))
    assert qty == 300


# ── compute_futures_qty ───────────────────────────────────────────────────────

def test_compute_futures_qty_normal():
    """floor(10000/1.0/2)*2 = 5000*2 = 10000."""
    assert compute_futures_qty(Decimal("10000"), Decimal("1.0"), Decimal("2")) == 10000


def test_compute_futures_qty_sub_lot_returns_0():
    """floor(1/1.0/10000)*10000 = 0 → returns 0."""
    assert compute_futures_qty(Decimal("1"), Decimal("1.0"), Decimal("10000")) == 0


def test_compute_futures_qty_zero_capital_returns_0():
    assert compute_futures_qty(Decimal("0"), Decimal("1.0"), Decimal("2")) == 0


# ── daily_loss_remaining ──────────────────────────────────────────────────────

def test_daily_loss_remaining_no_trades_returns_full_cap():
    """With no closed trades today, remaining = full cap = min(2000, 100000*2%) = 2000."""
    factory = _make_factory()
    s = _settings()
    with factory() as session:
        with patch("app.risk.get_settings", return_value=s):
            result = daily_loss_remaining(session, _now())
    assert result == Decimal("2000")


def test_daily_loss_remaining_partial_losses():
    """500 loss today → remaining = 2000 - 500 = 1500."""
    factory = _make_factory()
    s = _settings()
    with factory() as session:
        _seed_closed_trade(session, pnl=-500.0)
        session.commit()
        with patch("app.risk.get_settings", return_value=s):
            result = daily_loss_remaining(session, _now())
    assert result == Decimal("1500")


def test_daily_loss_remaining_exhausted_returns_zero():
    """3000 total loss exceeds 2000 cap → remaining = 0 (floored)."""
    factory = _make_factory()
    s = _settings()
    with factory() as session:
        _seed_closed_trade(session, pnl=-1500.0)
        _seed_closed_trade(session, pnl=-1500.0)
        session.commit()
        with patch("app.risk.get_settings", return_value=s):
            result = daily_loss_remaining(session, _now())
    assert result == Decimal("0")


# ── check_risk_gates ─────────────────────────────────────────────────────────

def test_check_risk_gates_daily_loss_gate_fires():
    """daily_loss_remaining == 0 → RiskHaltError."""
    factory = _make_factory()
    s = _settings()
    with factory() as session:
        _seed_closed_trade(session, pnl=-3000.0)   # exceeds 2000 cap
        session.commit()
        with patch("app.risk.get_settings", return_value=s):
            with pytest.raises(RiskHaltError) as exc_info:
                check_risk_gates(session, _now())
    assert "daily" in exc_info.value.reason.lower()


def test_check_risk_gates_consecutive_loss_gate_fires_at_3():
    """Exactly 3 consecutive losses triggers the circuit breaker."""
    from datetime import timedelta
    factory = _make_factory()
    s = _settings(MAX_DAILY_LOSS=Decimal("100000"))  # large cap so daily gate doesn't fire
    base = _now()
    with factory() as session:
        _seed_closed_trade(session, pnl=-100.0, closed_at=base)
        _seed_closed_trade(session, pnl=-200.0, closed_at=base + timedelta(seconds=1))
        _seed_closed_trade(session, pnl=-300.0, closed_at=base + timedelta(seconds=2))
        session.commit()
        with patch("app.risk.get_settings", return_value=s):
            with pytest.raises(RiskHaltError) as exc_info:
                check_risk_gates(session, _now())
    assert "consecutive" in exc_info.value.reason.lower()


def test_check_risk_gates_resets_after_winner():
    """3 losses then 1 win (most recent) → consecutive run = 0 → gate does NOT fire."""
    from datetime import timedelta
    factory = _make_factory()
    s = _settings(MAX_DAILY_LOSS=Decimal("100000"))
    base = _now()
    with factory() as session:
        _seed_closed_trade(session, pnl=-100.0, closed_at=base)
        _seed_closed_trade(session, pnl=-200.0, closed_at=base + timedelta(seconds=1))
        _seed_closed_trade(session, pnl=-300.0, closed_at=base + timedelta(seconds=2))
        _seed_closed_trade(session, pnl=+400.0, closed_at=base + timedelta(seconds=3))  # most recent
        session.commit()
        with patch("app.risk.get_settings", return_value=s):
            check_risk_gates(session, _now())   # must not raise


def test_check_risk_gates_passes_when_clear():
    """No trades → no gates triggered."""
    factory = _make_factory()
    s = _settings()
    with factory() as session:
        with patch("app.risk.get_settings", return_value=s):
            check_risk_gates(session, _now())   # must not raise


# ── record_trade_result ───────────────────────────────────────────────────────

def test_record_trade_result_long_position():
    """Long: fill=130, entry=100, qty=50 → PnL = (130-100)*50 = 1500."""
    factory = _make_factory()
    with factory() as session:
        order, pos = _seed_entry(session, kite_order_id="ORD_LONG")
        session.commit()

    with factory() as session:
        pnl = Decimal("1500")  # (130-100)*50
        record_trade_result(session, "ORD_LONG", pnl, _now())
        session.commit()

    with factory() as session:
        ct = session.query(ClosedTrade).first()
    assert ct is not None
    assert ct.pnl == pytest.approx(1500.0)


def test_record_trade_result_short_position():
    """Short: fill=70 (covered), entry=100, qty=50 → PnL = -(70-100)*50 = 1500 (profit)."""
    factory = _make_factory()
    with factory() as session:
        order, pos = _seed_entry(session, kite_order_id="ORD_SHORT")
        session.commit()

    with factory() as session:
        pnl = Decimal("1500")  # sign already computed by caller
        record_trade_result(session, "ORD_SHORT", pnl, _now())
        session.commit()

    with factory() as session:
        ct = session.query(ClosedTrade).first()
    assert ct.pnl == pytest.approx(1500.0)


# ── main.py dispatcher: RiskHaltError path ───────────────────────────────────

def _make_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_alert_row(session, tv_ticker: str = "NIFTY") -> Alert:
    now = _now()
    row = Alert(
        received_at=now,
        strategy_id="test",
        tv_ticker=tv_ticker,
        tv_exchange="NSE",
        action="BUY",
        product="NRML",
        idempotency_key=f"ik_{_secrets.token_hex(6)}",
        raw_payload="{}",
        processed=False,
    )
    session.add(row)
    session.flush()
    return row


def _alert_data(tv_ticker: str = "NIFTY") -> MagicMock:
    from app.webhook_models import Action, TradingViewAlert
    return TradingViewAlert(
        secret="sec",
        strategy_id="test",
        tv_ticker=tv_ticker,
        tv_exchange="NSE",
        action=Action.BUY,
        product="NRML",
        time="2026-04-27T10:00:00",
        bar_time="2026-04-27T09:45:00",
    )


def test_risk_halt_error_causes_early_return_no_broker_calls():
    """When check_risk_gates raises, _process_alert exits before any broker call."""
    factory = _make_session_factory()
    s = Settings(
        _env_file=None, DRY_RUN=False, KITE_API_KEY="k", DATABASE_URL="sqlite:///:memory:",
        WEBHOOK_SECRET="sec", SECRET_KEY="", PBKDF2_ITERATIONS=1,
        RISK_PCT=Decimal("0.01"), MAX_DAILY_LOSS=Decimal("2000"),
        SL_PERCENT=Decimal("0.005"),
    )

    with factory() as session:
        alert = _seed_alert_row(session, tv_ticker="NIFTY")
        alert_id = alert.id
        session.commit()

    main_module._SessionFactory = factory

    with (
        patch("app.risk.check_risk_gates", side_effect=RiskHaltError("daily loss cap exhausted")),
        patch("app.main.place_entry") as mock_pe,
        patch("app.main.select_strike") as mock_ss,
    ):
        _process_alert(alert_id, _alert_data("NIFTY"), s)

    mock_pe.assert_not_called()
    mock_ss.assert_not_called()


def test_dry_run_bypasses_check_risk_gates():
    """DRY_RUN=True: check_risk_gates must not be called at all."""
    factory = _make_session_factory()
    s = Settings(
        _env_file=None, DRY_RUN=True, KITE_API_KEY="k", DATABASE_URL="sqlite:///:memory:",
        WEBHOOK_SECRET="sec", SECRET_KEY="", PBKDF2_ITERATIONS=1,
        RISK_PCT=Decimal("0.01"), MAX_DAILY_LOSS=Decimal("2000"),
        SL_PERCENT=Decimal("0.005"),
    )

    with factory() as session:
        alert = _seed_alert_row(session, tv_ticker="NIFTY")
        alert_id = alert.id
        session.commit()

    main_module._SessionFactory = factory

    with patch("app.risk.check_risk_gates") as mock_crg:
        _process_alert(alert_id, _alert_data("NIFTY"), s)

    mock_crg.assert_not_called()


# ── GttFilledEvent → record_trade_result wiring ───────────────────────────────

def test_gtt_filled_event_fires_on_sell_complete():
    """OrderWatcher.on_order_update with COMPLETE SELL fires on_gtt_filled callback."""
    from app.watcher import OrderWatcher

    received: list[GttFilledEvent] = []

    def _capture(evt: GttFilledEvent):
        received.append(evt)

    watcher = OrderWatcher(on_gtt_filled=_capture)
    watcher.on_order_update(
        ws=None,
        data={
            "order_id": "GTT_CHILD_1",
            "status": "COMPLETE",
            "transaction_type": "SELL",
            "tradingsymbol": "NIFTY25APR19500CE",
            "average_price": 130.0,
            "filled_quantity": 50,
        },
    )

    assert len(received) == 1
    assert received[0].kite_order_id == "GTT_CHILD_1"
    assert received[0].tradingsymbol == "NIFTY25APR19500CE"
    assert received[0].fill_price == pytest.approx(130.0)
    assert received[0].transaction_type == "SELL"


def test_on_gtt_filled_writes_pnl_to_closed_trade():
    """_on_gtt_filled: SELL fill at 130 against entry 100 × 50 qty → PnL = 1500."""
    factory = _make_session_factory()
    kite_oid = "ORD_GTT_ENTRY"

    with factory() as session:
        order, pos = _seed_entry(session, tradingsymbol="NIFTY_GTT", kite_order_id=kite_oid)
        session.commit()

    main_module._SessionFactory = factory

    event = GttFilledEvent(
        kite_order_id="GTT_CHILD_99",
        tradingsymbol="NIFTY_GTT",
        fill_price=130.0,
        fill_qty=50,
        transaction_type="SELL",
    )
    _on_gtt_filled(event)

    with factory() as session:
        ct = session.query(ClosedTrade).first()
    assert ct is not None
    assert ct.pnl == pytest.approx(1500.0)   # (130-100)*50
