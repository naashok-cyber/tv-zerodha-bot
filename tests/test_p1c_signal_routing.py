"""Tests for P1-c signal routing in app/main.py — all offline, no Kite calls."""
from __future__ import annotations

import secrets as _secrets
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
import app.state as state
from app.config import IST, UTC, Settings
from app.expiry_resolver import ResolvedExpiry
from app.main import (
    _check_risk_guards,
    _on_entry_filled,
    _process_alert,
    app,
    get_current_settings,
    get_db_session,
)
from app.schemas import AlertPayload
from app.storage import Alert, Base, ClosedTrade, Gtt, Instrument, Order, Position
from app.watcher import EntryFilledEvent


# ── Shared helpers ────────────────────────────────────────────────────────────

_TV_IP = "52.89.214.238"
_SECRET = "test-hmac-secret"


def _s(**kwargs) -> Settings:
    defaults = dict(
        _env_file=None,
        DRY_RUN=True,
        CAPITAL_PER_TRADE=10_000.0,
        RISK_PER_TRADE_PCT=100.0,    # legacy; kept for compatibility
        RISK_PCT=Decimal("1.0"),     # 100% fraction so capital_risk == CAPITAL_PER_TRADE
        MAX_DAILY_LOSS_ABS=100_000.0,  # large enough to never constrain sizing in tests
        MAX_DAILY_LOSS=Decimal("100000"),  # risk.py Decimal cap; large so it doesn't bind
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


def _make_alert(
    action: str = "BUY",
    symbol: str = "NIFTY",
    price: float = 19_500.0,
    premium: float | None = None,
) -> AlertPayload:
    return AlertPayload(
        symbol=symbol,
        action=action,
        price=Decimal(str(price)),
        premium=Decimal(str(premium)) if premium is not None else None,
        timeframe="5",
        alert_id="test_alert_001",
        timestamp=datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC),
    )


def _make_ng_alert(price: float = 200.0) -> AlertPayload:
    return _make_alert("BUY", symbol="NATURALGAS", price=price)


def _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix: str = "") -> Alert:
    """Insert a minimal Alert row and return it (flushed but not committed)."""
    row = Alert(
        received_at=datetime.now(IST),
        strategy_id="seed",
        tv_ticker=tv_ticker,
        tv_exchange="NSE",
        action=action,
        product="NRML",
        idempotency_key=f"seed_{tv_ticker}_{suffix}_{_secrets.token_hex(4)}",
        raw_payload="{}",
        processed=False,
    )
    session.add(row)
    session.flush()
    return row


def _seed_instrument(
    session,
    name: str = "NIFTY",
    exchange: str = "NFO",
    itype: str = "CE",
    lot_size: int = 1,
    tick_size: float = 0.05,
    strike: float = 19500.0,
    expiry: date | None = None,
    tradingsymbol: str | None = None,
) -> Instrument:
    ts = tradingsymbol or f"{name}TESTOPT"
    inst = Instrument(
        instrument_token=abs(hash(ts)) % (2**31),
        exchange_token=1,
        tradingsymbol=ts,
        name=name,
        expiry=expiry or date(2026, 5, 1),
        strike=strike,
        tick_size=tick_size,
        lot_size=lot_size,
        instrument_type=itype,
        segment=exchange,
        exchange=exchange,
    )
    session.add(inst)
    session.flush()
    return inst


def _seed_open_position(
    session,
    tradingsymbol: str = "NIFTY",
    exchange: str = "NFO",
    entry_premium: float = 100.0,
    current_sl: float = 70.0,
    qty: int = 50,
    ikey_suffix: str = "",
) -> tuple[Position, Gtt]:
    """Seed Alert → Order → Gtt → Position for EXIT/TRAIL tests."""
    now = datetime.now(IST)
    alert = _seed_alert(session, tv_ticker=tradingsymbol, suffix=ikey_suffix or tradingsymbol)

    order = Order(
        alert_id=alert.id,
        kite_order_id=f"ORD_{tradingsymbol}_{_secrets.token_hex(3)}",
        variety="regular",
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        transaction_type="BUY",
        order_type="MARKET",
        product="NRML",
        quantity=qty,
        status="COMPLETE",
        placed_at=now,
        updated_at=now,
        dry_run=False,
    )
    session.add(order)
    session.flush()

    target_price = entry_premium + 2 * (entry_premium - current_sl)
    gtt = Gtt(
        order_id=order.id,
        kite_gtt_id=99999,
        gtt_type="OCO",
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        sl_trigger=current_sl,
        target_trigger=target_price,
        sl_order_price=current_sl,
        target_order_price=target_price,
        last_price_at_placement=entry_premium,
        status="ACTIVE",
        placed_at=now,
        updated_at=now,
        dry_run=False,
    )
    session.add(gtt)
    session.flush()

    pos = Position(
        order_id=order.id,
        gtt_id=gtt.id,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        underlying=tradingsymbol,
        instrument_type="CE",
        entry_premium=entry_premium,
        current_sl=current_sl,
        quantity=qty,
        lot_size=1,
        opened_at=now,
        last_updated_at=now,
    )
    session.add(pos)
    session.commit()
    return pos, gtt


# ── NATURALGAS branch ─────────────────────────────────────────────────────────

def test_ng_sizing_math():
    """qty is capped at MAX_LOTS_PER_TRADE(1) * lot_size unconditionally."""
    # capital_risk = 15_000 * 0.05 = 750; sl_per_contract = 200 * 0.008 * 1250 = 2000
    # raw qty = floor(750 / 2000 / 2) * 2 = 0 → but MAX_LOTS_PER_TRADE clamps after sizing.
    # With RISK_PCT=1.0: capital_risk=15000, qty=floor(15000/2000/2)*2=6; capped to 1*2=2.
    factory = _make_factory()
    s = _s(CAPITAL_PER_TRADE=15_000.0, RISK_PCT=Decimal("1.0"), FUTURES_SL_PCT=0.005)

    with factory() as session:
        _seed_instrument(session, name="NATURALGAS", exchange="MCX", itype="FUT",
                         lot_size=2, expiry=date(2026, 5, 19), tradingsymbol="NATGAS_FUT")
        alert = _seed_alert(session, tv_ticker="NATURALGAS", action="BUY", suffix="sz")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 19), days_to_expiry=22, rule_used="NEAREST_MONTHLY")
    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.place_entry") as mock_pe,
        patch("app.risk.daily_loss_remaining", return_value=Decimal("100000")),
    ):
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_ng_alert(price=200.0), s)

    # DRY_RUN=True — place_entry not called; Order row carries the computed qty
    with factory() as session:
        order = session.query(Order).filter_by(alert_id=alert_id).first()
    assert order is not None
    assert order.dry_run is True
    assert order.quantity == 2         # capped: MAX_LOTS_PER_TRADE(1) * lot_size(2)
    mock_pe.assert_not_called()


def test_ng_dry_run_skips_place_entry():
    """DRY_RUN=True must not call place_entry."""
    factory = _make_factory()
    s = _s(DRY_RUN=True)

    with factory() as session:
        _seed_instrument(session, name="NATURALGAS", exchange="MCX", itype="FUT",
                         lot_size=1, expiry=date(2026, 5, 19), tradingsymbol="NATGAS_FUT2")
        alert = _seed_alert(session, tv_ticker="NATURALGAS", action="BUY", suffix="dry")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 19), days_to_expiry=22, rule_used="NEAREST_MONTHLY")
    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.place_entry") as mock_pe,
    ):
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_ng_alert(), s)

    mock_pe.assert_not_called()


def test_ng_live_calls_place_entry_with_correct_args():
    """DRY_RUN=False: place_entry called with FUT instrument, BUY, qty, MARKET."""
    factory = _make_factory()
    # RISK_PCT=100% → capital_risk=10000; SL_PERCENT=0.008; mcx_units=1250; lot_size=1
    # sl_per_contract=200*0.008*1250=2000; raw qty=floor(10000/2000/1)*1=5; capped to MAX_LOTS_PER_TRADE(1)=1
    s = _s(DRY_RUN=False, CAPITAL_PER_TRADE=10_000.0, RISK_PCT=Decimal("1.0"))

    with factory() as session:
        _seed_instrument(session, name="NATURALGAS", exchange="MCX", itype="FUT",
                         lot_size=1, expiry=date(2026, 5, 19), tradingsymbol="NATGAS_FUT3")
        alert = _seed_alert(session, tv_ticker="NATURALGAS", action="BUY", suffix="live")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 19), days_to_expiry=22, rule_used="NEAREST_MONTHLY")
    mock_kite = MagicMock()

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.place_entry", return_value="ORD_NG") as mock_pe,
        patch("app.main.get_session_manager") as mock_sm,
        patch("app.risk.daily_loss_remaining", return_value=Decimal("100000")),
        patch("app.risk.check_risk_gates"),  # skip gates for live-path test
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_ng_alert(price=200.0), s)

    mock_pe.assert_called_once()
    args = mock_pe.call_args.args
    assert args[2] == "BUY"
    assert args[3] == 1          # capped: MAX_LOTS_PER_TRADE(1) * lot_size(1)
    assert args[4] == "MARKET"


def test_ng_sell_signal_places_sell_order():
    """SELL signal must place a SELL (short) futures order, not BUY."""
    factory = _make_factory()
    s = _s(DRY_RUN=False, CAPITAL_PER_TRADE=10_000.0, RISK_PER_TRADE_PCT=100.0, FUTURES_SL_PCT=0.005)

    with factory() as session:
        _seed_instrument(session, name="NATURALGAS", exchange="MCX", itype="FUT",
                         lot_size=1, expiry=date(2026, 5, 19), tradingsymbol="NATGAS_FUT_SELL")
        alert = _seed_alert(session, tv_ticker="NATURALGAS", action="SELL", suffix="sell")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 19), days_to_expiry=22, rule_used="NEAREST_MONTHLY")
    mock_kite = MagicMock()

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.place_entry", return_value="ORD_NG_SELL") as mock_pe,
        patch("app.main.get_session_manager") as mock_sm,
        patch("app.risk.daily_loss_remaining", return_value=Decimal("100000")),
        patch("app.risk.check_risk_gates"),
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        sell_alert = _make_alert("SELL", symbol="NATURALGAS", price=200.0)
        _process_alert(alert_id, sell_alert, s)

    mock_pe.assert_called_once()
    args = mock_pe.call_args.args
    assert args[2] == "SELL"

    with factory() as session:
        order = session.query(Order).filter_by(alert_id=alert_id).first()
    assert order is not None
    assert order.transaction_type == "SELL"


def test_ng_insufficient_capital_rejected(caplog):
    """When qty < lot_size, entry is rejected with a warning log."""
    import logging
    factory = _make_factory()
    # capital_risk=100, sl_distance=1.0, lot_size=10000 → floor=0 < 10000 → reject
    s = _s(DRY_RUN=True, CAPITAL_PER_TRADE=100.0, RISK_PER_TRADE_PCT=100.0, FUTURES_SL_PCT=0.005)

    with factory() as session:
        _seed_instrument(session, name="NATURALGAS", exchange="MCX", itype="FUT",
                         lot_size=10_000, expiry=date(2026, 5, 19), tradingsymbol="NATGAS_FTBIG")
        alert = _seed_alert(session, tv_ticker="NATURALGAS", action="BUY", suffix="rej")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 19), days_to_expiry=22, rule_used="NEAREST_MONTHLY")
    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.place_entry") as mock_pe,
    ):
        with caplog.at_level(logging.WARNING, logger="app.main"):
            main_module._SessionFactory = factory
            _process_alert(alert_id, _make_ng_alert(price=200.0), s)

    mock_pe.assert_not_called()
    assert any("insufficient capital" in r.message.lower() for r in caplog.records)


# ── Non-NG options entry ──────────────────────────────────────────────────────

def _mock_selection(ltp: float = 50.0, lot_size: int = 50, delta: float = 0.65) -> MagicMock:
    inst = MagicMock()
    inst.lot_size = lot_size
    inst.exchange = "NFO"
    inst.tradingsymbol = "NIFTY25APR19500CE"
    sel = MagicMock()
    sel.instrument = inst
    sel.option_ltp = ltp
    sel.computed_delta = delta
    return sel


def test_non_ng_buy_routes_to_ce():
    """BUY action must pass flag='CE' to select_strike."""
    factory = _make_factory()
    s = _s(DRY_RUN=False)

    with factory() as session:
        alert = _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix="ce")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 1), days_to_expiry=4, rule_used="NEAREST_WEEKLY")
    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 19_500.0}}

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.select_strike", return_value=_mock_selection()) as mock_ss,
        patch("app.main.place_entry", return_value="ORD1"),
        patch("app.main.get_session_manager") as mock_sm,
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("BUY", symbol="NIFTY"), s)

    assert mock_ss.call_args.args[2] == "CE"


def test_non_ng_sell_routes_to_pe():
    """SELL action must pass flag='PE' to select_strike."""
    factory = _make_factory()
    s = _s(DRY_RUN=False)

    with factory() as session:
        alert = _seed_alert(session, tv_ticker="NIFTY", action="SELL", suffix="pe")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 1), days_to_expiry=4, rule_used="NEAREST_WEEKLY")
    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 19_500.0}}

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.select_strike", return_value=_mock_selection()) as mock_ss,
        patch("app.main.place_entry", return_value="ORD2"),
        patch("app.main.get_session_manager") as mock_sm,
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("SELL", symbol="NIFTY"), s)

    assert mock_ss.call_args.args[2] == "PE"


def test_non_ng_sizing_math():
    """qty is risk-bounded then hard-capped at MAX_LOTS_PER_TRADE(1) * lot_size.

    RISK_PCT=5%, CAPITAL=100,000 → capital_risk=5,000.
    ltp=50, lot_size=50, SL_PCT=0.30:
      sl_per_unit = 50 * 0.30 = 15
      lots = floor(5,000 / 15 / 50) = 6  → qty=300
      cap  = MAX_LOTS_PER_TRADE(1) * lot_size(50) = 50
      final qty = min(300, 50) = 50
    """
    factory = _make_factory()
    s = _s(
        DRY_RUN=False,
        CAPITAL_PER_TRADE=100_000.0,
        RISK_PCT=Decimal("0.05"),
        SL_PREMIUM_PCT=0.30,
    )

    with factory() as session:
        alert = _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix="sz2")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 1), days_to_expiry=4, rule_used="NEAREST_WEEKLY")
    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 19_500.0}}

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.select_strike", return_value=_mock_selection(ltp=50.0, lot_size=50)),
        patch("app.main.place_entry", return_value="ORD3"),
        patch("app.main.get_session_manager") as mock_sm,
        patch("app.risk.daily_loss_remaining", return_value=Decimal("100000")),
        patch("app.risk.check_risk_gates"),
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("BUY"), s)

    with factory() as session:
        order = session.query(Order).filter_by(alert_id=alert_id).first()
    assert order.quantity == 50    # capped at MAX_LOTS_PER_TRADE(1)*lot_size(50)


def test_non_ng_dry_run_skips_broker_calls():
    """DRY_RUN=True: select_strike and place_entry must not be called."""
    factory = _make_factory()
    s = _s(DRY_RUN=True)

    with factory() as session:
        alert = _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix="dr2")
        alert_id = alert.id
        session.commit()

    with (
        patch("app.main.select_strike") as mock_ss,
        patch("app.main.place_entry") as mock_pe,
    ):
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("BUY"), s)

    mock_ss.assert_not_called()
    mock_pe.assert_not_called()


def test_non_ng_max_loss_capped_at_risk_budget():
    """Max loss per trade = qty × sl_per_unit ≤ CAPITAL × RISK_PCT (≤₹5,000 at defaults)."""
    # CAPITAL=100,000, RISK_PCT=5% → capital_risk=5,000.
    # ltp=100, lot_size=75, SL_PCT=0.30 → sl_per_unit=30, lots=floor(5000/30/75)=2, qty=150.
    # cap = MAX_LOTS_PER_TRADE(1) * lot_size(75) = 75  → final qty=75.
    # max_loss = 30 × 75 = 2,250 ≤ 5,000  ✓
    factory = _make_factory()
    s = _s(
        DRY_RUN=False,
        CAPITAL_PER_TRADE=100_000.0,
        RISK_PCT=Decimal("0.05"),
        SL_PREMIUM_PCT=0.30,
    )

    with factory() as session:
        alert = _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix="riskmax")
        alert_id = alert.id
        session.commit()

    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 1), days_to_expiry=4, rule_used="NEAREST_WEEKLY")
    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 19_500.0}}

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.select_strike", return_value=_mock_selection(ltp=100.0, lot_size=75)),
        patch("app.main.place_entry", return_value="ORD_RISK"),
        patch("app.main.get_session_manager") as mock_sm,
        patch("app.risk.check_risk_gates"),
        patch("app.risk.daily_loss_remaining", return_value=Decimal("100000")),
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("BUY", symbol="NIFTY"), s)

    with factory() as session:
        order = session.query(Order).filter_by(alert_id=alert_id).first()
    assert order is not None
    assert order.quantity == 75    # 1 lot × 75 (MAX_LOTS_PER_TRADE cap)

    # Verify max loss ≤ risk budget
    sl_per_unit = 100.0 * 0.30   # ₹30/unit
    max_loss = sl_per_unit * order.quantity
    assert max_loss <= 100_000 * 0.05   # ₹2,250 ≤ ₹5,000


# ── EXIT branch ───────────────────────────────────────────────────────────────

def test_exit_cancel_before_squareoff():
    """cancel_gtt must be called before square_off."""
    factory = _make_factory()
    s = _s(DRY_RUN=False)

    with factory() as session:
        # Instrument tradingsymbol must match ts = underlying.name = "NIFTY"
        _seed_instrument(session, name="NIFTY", exchange="NFO", itype="CE",
                         lot_size=1, tradingsymbol="NIFTY")
        _seed_open_position(session, tradingsymbol="NIFTY", ikey_suffix="exit_order")
        alert = _seed_alert(session, tv_ticker="NIFTY", action="EXIT", suffix="ex")
        alert_id = alert.id
        session.commit()

    call_order: list[str] = []
    mock_kite = MagicMock()

    def fake_cancel(*args, **kwargs):
        call_order.append("cancel_gtt")
        return True

    def fake_square(*args, **kwargs):
        call_order.append("square_off")
        return "ORD_SO"

    with (
        patch("app.main.cancel_gtt", fake_cancel),
        patch("app.main.square_off", fake_square),
        patch("app.main.get_session_manager") as mock_sm,
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("EXIT", symbol="NIFTY"), s)

    assert "cancel_gtt" in call_order
    assert "square_off" in call_order
    assert call_order.index("cancel_gtt") < call_order.index("square_off")


def test_exit_dry_run_skips_broker_calls():
    """DRY_RUN=True: cancel_gtt and square_off must not be called."""
    factory = _make_factory()
    s = _s(DRY_RUN=True)

    with factory() as session:
        _seed_instrument(session, name="NIFTY", exchange="NFO", itype="CE",
                         lot_size=1, tradingsymbol="NIFTY")
        _seed_open_position(session, tradingsymbol="NIFTY", ikey_suffix="exit_dry")
        alert = _seed_alert(session, tv_ticker="NIFTY", action="EXIT", suffix="exd")
        alert_id = alert.id
        session.commit()

    with (
        patch("app.main.cancel_gtt") as mock_cg,
        patch("app.main.square_off") as mock_so,
    ):
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("EXIT", symbol="NIFTY"), s)

    mock_cg.assert_not_called()
    mock_so.assert_not_called()


# ── TRAIL branch ──────────────────────────────────────────────────────────────

# ── EntryFilledEvent → GTT OCO ────────────────────────────────────────────────

def test_entry_filled_options_gtt_prices():
    """fill_price=100, SL_PREMIUM_PCT=0.30: sl=70, target=160; DRY_RUN skips place_gtt_oco."""
    factory = _make_factory()
    s = _s(DRY_RUN=True, SL_PREMIUM_PCT=0.30, RR_RATIO=2.0)

    with factory() as session:
        inst = _seed_instrument(session, name="NIFTY", exchange="NFO", itype="CE",
                                lot_size=50, tradingsymbol="NIFTY_OPT_TEST")
        seed_alert = _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix="ef")
        order = Order(
            alert_id=seed_alert.id,
            kite_order_id="ORD_FILL",
            variety="regular",
            exchange="NFO",
            tradingsymbol=inst.tradingsymbol,
            transaction_type="BUY",
            order_type="MARKET",
            product="NRML",
            quantity=50,
            status="PENDING",
            placed_at=datetime.now(IST),
            updated_at=datetime.now(IST),
            dry_run=False,
        )
        session.add(order)
        session.commit()

    main_module._SessionFactory = factory
    main_module._pending_order_meta["ORD_FILL"] = {
        "instrument_type": "CE",
        "underlying": "NIFTY",
    }

    event = EntryFilledEvent(kite_order_id="ORD_FILL", fill_price=100.0, fill_qty=50)

    with (
        patch("app.main.get_settings", return_value=s),
        patch("app.main.place_gtt_oco") as mock_gtt,
    ):
        _on_entry_filled(event)

    with factory() as session:
        pos = session.query(Position).first()
    assert pos is not None
    assert pos.entry_premium == pytest.approx(100.0)
    assert pos.current_sl == pytest.approx(70.0)   # 100 - 0.30*100

    mock_gtt.assert_not_called()   # DRY_RUN=True


def test_entry_filled_futures_gtt_prices():
    """Long FUT fill: sl = fill_price*(1-FUTURES_SL_PCT), target above entry."""
    factory = _make_factory()
    # FUTURES_SL_PCT=0.005 → sl_distance=200*0.005=1.0 → sl=199.0, target=202.0 (RR=2)
    s = _s(DRY_RUN=True, FUTURES_SL_PCT=0.005)

    with factory() as session:
        inst = _seed_instrument(session, name="NATURALGAS", exchange="MCX", itype="FUT",
                                lot_size=1, tradingsymbol="NATGAS_FUT_EF")
        seed_alert = _seed_alert(session, tv_ticker="NATURALGAS", action="BUY", suffix="ef2")
        order = Order(
            alert_id=seed_alert.id,
            kite_order_id="ORD_FUT_FILL",
            variety="regular",
            exchange="MCX",
            tradingsymbol=inst.tradingsymbol,
            transaction_type="BUY",
            order_type="MARKET",
            product="NRML",
            quantity=100,
            status="PENDING",
            placed_at=datetime.now(IST),
            updated_at=datetime.now(IST),
            dry_run=False,
        )
        session.add(order)
        session.commit()

    main_module._SessionFactory = factory
    main_module._pending_order_meta["ORD_FUT_FILL"] = {
        "instrument_type": "FUT",
        "entry_side": "BUY",
        "underlying": "NATURALGAS",
    }

    event = EntryFilledEvent(kite_order_id="ORD_FUT_FILL", fill_price=200.0, fill_qty=100)

    with (
        patch("app.main.get_settings", return_value=s),
        patch("app.main.place_gtt_oco") as mock_gtt,
    ):
        _on_entry_filled(event)

    with factory() as session:
        pos = session.query(Position).first()
        gtt = session.query(Gtt).first()
    assert pos.current_sl == pytest.approx(199.0)   # 200 - 200*0.005
    assert gtt.sl_trigger == pytest.approx(199.0)
    assert gtt.target_trigger == pytest.approx(202.0)   # 200 + 2*(200*0.005)
    mock_gtt.assert_not_called()   # DRY_RUN=True


def test_entry_filled_futures_sell_gtt_prices():
    """Short FUT fill: sl ABOVE entry, target BELOW entry."""
    factory = _make_factory()
    s = _s(DRY_RUN=True, FUTURES_SL_PCT=0.005)

    with factory() as session:
        inst = _seed_instrument(session, name="NATURALGAS", exchange="MCX", itype="FUT",
                                lot_size=1, tradingsymbol="NATGAS_FUT_SHORT")
        seed_alert = _seed_alert(session, tv_ticker="NATURALGAS", action="SELL", suffix="ef3")
        order = Order(
            alert_id=seed_alert.id,
            kite_order_id="ORD_FUT_SHORT",
            variety="regular",
            exchange="MCX",
            tradingsymbol=inst.tradingsymbol,
            transaction_type="SELL",
            order_type="MARKET",
            product="NRML",
            quantity=100,
            status="PENDING",
            placed_at=datetime.now(IST),
            updated_at=datetime.now(IST),
            dry_run=False,
        )
        session.add(order)
        session.commit()

    main_module._SessionFactory = factory
    main_module._pending_order_meta["ORD_FUT_SHORT"] = {
        "instrument_type": "FUT",
        "entry_side": "SELL",
        "underlying": "NATURALGAS",
    }

    event = EntryFilledEvent(kite_order_id="ORD_FUT_SHORT", fill_price=200.0, fill_qty=100)

    with (
        patch("app.main.get_settings", return_value=s),
        patch("app.main.place_gtt_oco") as mock_gtt,
    ):
        _on_entry_filled(event)

    with factory() as session:
        pos = session.query(Position).first()
        gtt = session.query(Gtt).first()
    assert pos.current_sl == pytest.approx(201.0)   # 200 + 200*0.005 — SL above entry for short
    assert gtt.sl_trigger == pytest.approx(201.0)
    assert gtt.target_trigger == pytest.approx(198.0)   # 200 - 2*(200*0.005) — target below entry
    mock_gtt.assert_not_called()   # DRY_RUN=True


# ── /kite/callback wires watcher.restart() ───────────────────────────────────

def test_kite_callback_starts_watcher():
    """Successful /kite/callback must call _watcher.restart(api_key, access_token)."""
    engine = _make_engine()
    factory = _make_factory(engine)
    settings = _s(KITE_API_KEY="testkey")

    # Patch OrderWatcher class so lifespan creates our mock when it runs
    # `_watcher = OrderWatcher(on_entry_filled=...)`.
    mock_watcher_instance = MagicMock()

    def _override_db():
        with factory() as sess:
            yield sess

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_current_settings] = lambda: settings

    with (
        patch.object(main_module, "_SessionFactory", factory),
        patch("app.main.OrderWatcher", return_value=mock_watcher_instance),
        patch("app.main.get_session_manager") as mock_sm,
        patch("app.main.get_settings", return_value=settings),
    ):
        mock_sm.return_value.handle_callback.return_value = "acc_token_xyz"
        with TestClient(app) as client:
            resp = client.get("/kite/callback?status=success&request_token=req123")

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    mock_watcher_instance.restart.assert_called_once_with(
        api_key="testkey", access_token="acc_token_xyz"
    )


# ── Delta fallback when primary strike exceeds capital ────────────────────────

from app.strike_selector import NoValidStrikeError as _NoValidStrikeError


def test_options_delta_fallback_succeeds_on_lower_delta():
    """When primary strike SL exceeds risk budget, bot retries at lower delta and places the order.

    capital_risk = 5,000 * 100% = 5,000.
    Primary (ltp=400, lot_size=50): sl_per_lot = 400*0.30*50 = 6,000 > 5,000 → 0 lots.
    Fallback delta=0.50 (ltp=100, lot_size=50): sl_per_lot = 100*0.30*50 = 1,500 → 3 lots (qty=150).
    cap = MAX_LOTS_PER_TRADE(1) * lot_size(50) = 50 → final qty=50.
    """
    factory = _make_factory()
    s = _s(
        DRY_RUN=False,
        CAPITAL_PER_TRADE=5_000.0,
        RISK_PCT=Decimal("1.0"),
        SL_PREMIUM_PCT=0.30,
        TARGET_DELTA=0.65,
        DELTA_FALLBACK_STEPS=[0.50, 0.35],
    )

    with factory() as session:
        alert = _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix="fb1")
        alert_id = alert.id
        session.commit()

    expensive = _mock_selection(ltp=400.0, lot_size=50, delta=0.65)
    expensive.instrument.tradingsymbol = "NIFTY26APR26CE19500"
    affordable = _mock_selection(ltp=100.0, lot_size=50, delta=0.50)
    affordable.instrument.tradingsymbol = "NIFTY26APR26CE19700"

    def _ss_side_effect(*args, **kwargs):
        td = kwargs.get("target_delta", 0.65)
        if td == 0.65:
            return expensive
        if td == 0.50:
            return affordable
        raise _NoValidStrikeError("no strike")

    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 19_600.0}}
    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 1), days_to_expiry=4, rule_used="NEAREST_WEEKLY")

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.select_strike", side_effect=_ss_side_effect),
        patch("app.main.place_entry", return_value="ORD_FB") as mock_pe,
        patch("app.main.get_session_manager") as mock_sm,
        patch("app.risk.check_risk_gates"),
        patch("app.risk.daily_loss_remaining", return_value=Decimal("100000")),
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("BUY"), s)

    mock_pe.assert_called_once()
    assert mock_pe.call_args.args[1].tradingsymbol == "NIFTY26APR26CE19700"
    # sl_per_unit=30, qty=floor(5000/30/50)*50=150; cap=MAX_LOTS_PER_TRADE(1)*50=50
    assert mock_pe.call_args.args[3] == 50


def test_options_all_fallback_deltas_exhausted_forces_one_lot():
    """When every strike's SL exceeds the risk budget, bot forces 1 lot at the primary strike.

    capital_risk=5,000; all strikes ltp=400, lot_size=50:
      sl_per_lot = 400*0.30*50 = 6,000 > 5,000 → 0 lots at every delta
      → falls back to forcing 1 lot (qty=50) at primary strike rather than skipping.
    """
    state.set_session_invalid(False)  # reset state pollution from other tests
    state.set_paper_mode(None)  # clear override so settings.DRY_RUN=False is respected
    state.set_emergency_stop(False)
    state.set_entry_window_start(None)
    state.set_entry_window_end(None)
    factory = _make_factory()
    s = _s(
        DRY_RUN=False,
        CAPITAL_PER_TRADE=5_000.0,
        RISK_PCT=Decimal("1.0"),
        SL_PREMIUM_PCT=0.30,
        TARGET_DELTA=0.65,
        DELTA_FALLBACK_STEPS=[0.50, 0.35],
        ENTRY_WINDOW_START="09:00",
        ENTRY_WINDOW_END="16:00",  # alert timestamp is 10:00 UTC = 15:30 IST; must be inside window
    )

    with factory() as session:
        alert = _seed_alert(session, tv_ticker="NIFTY", action="BUY", suffix="fb2")
        alert_id = alert.id
        session.commit()

    def _ss_always_expensive(*args, **kwargs):
        return _mock_selection(ltp=400.0, lot_size=50, delta=kwargs.get("target_delta", 0.65))

    mock_kite = MagicMock()
    mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 19_600.0}}
    resolved = ResolvedExpiry(expiry_date=date(2026, 5, 1), days_to_expiry=4, rule_used="NEAREST_WEEKLY")

    with (
        patch("app.main.resolve_expiry", return_value=resolved),
        patch("app.main.select_strike", side_effect=_ss_always_expensive),
        patch("app.main.place_entry", return_value="ORD_FORCED") as mock_pe,
        patch("app.main.get_session_manager") as mock_sm,
        patch("app.risk.check_risk_gates"),
        patch("app.risk.daily_loss_remaining", return_value=Decimal("100000")),
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert("BUY"), s)

    mock_pe.assert_called_once()
    assert mock_pe.call_args.args[3] == 50  # 1 lot forced (lot_size=50)
