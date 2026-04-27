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
from app.config import IST, Settings
from app.expiry_resolver import ResolvedExpiry
from app.main import (
    _check_risk_guards,
    _on_entry_filled,
    _process_alert,
    app,
    get_current_settings,
    get_db_session,
)
from app.storage import Alert, Base, ClosedTrade, Gtt, Instrument, Order, Position
from app.watcher import EntryFilledEvent
from app.webhook_models import Action, TradingViewAlert


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
    action: Action = Action.BUY,
    tv_ticker: str = "NIFTY",
    entry_price: float = 19_500.0,
    sl_percent: float | None = None,
) -> TradingViewAlert:
    return TradingViewAlert(
        secret=_SECRET,
        strategy_id="test_strat",
        tv_ticker=tv_ticker,
        tv_exchange="NSE",
        action=action,
        product="NRML",
        entry_price=entry_price,
        sl_percent=sl_percent,
        time="2026-04-27T10:00:00",
        bar_time="2026-04-27T09:45:00",
    )


def _make_ng_alert(entry_price: float = 200.0, sl_percent: float | None = None) -> TradingViewAlert:
    return _make_alert(Action.BUY, tv_ticker="NATURALGAS", entry_price=entry_price, sl_percent=sl_percent)


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
    """qty = floor(capital_risk / sl_distance / lot_size) * lot_size."""
    # capital_risk = 10_000 (RISK_PCT=100%, daily cap=100k so not binding)
    # entry=200, sl_frac=0.005, sl_distance=1.0
    # qty = floor(10_000 / 1.0 / 2) * 2 = 5_000 * 2 = 10_000
    factory = _make_factory()
    s = _s(CAPITAL_PER_TRADE=10_000.0, RISK_PER_TRADE_PCT=100.0, FUTURES_SL_PCT=0.005)

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
        _process_alert(alert_id, _make_ng_alert(entry_price=200.0), s)

    # DRY_RUN=True — place_entry not called; Order row carries the computed qty
    with factory() as session:
        order = session.query(Order).filter_by(alert_id=alert_id).first()
    assert order is not None
    assert order.dry_run is True
    assert order.quantity == 10_000   # floor(10000/1.0/2)*2
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
    # capital_risk=10000, sl_distance=1.0, lot_size=1 → qty=10000
    s = _s(DRY_RUN=False, CAPITAL_PER_TRADE=10_000.0, RISK_PER_TRADE_PCT=100.0, FUTURES_SL_PCT=0.005)

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
        _process_alert(alert_id, _make_ng_alert(entry_price=200.0), s)

    mock_pe.assert_called_once()
    args = mock_pe.call_args.args
    assert args[2] == "BUY"
    assert args[3] == 10_000     # qty = floor(10000/1.0/1)*1
    assert args[4] == "MARKET"


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
            _process_alert(alert_id, _make_ng_alert(entry_price=200.0), s)

    mock_pe.assert_not_called()
    assert any("insufficient capital" in r.message.lower() for r in caplog.records)


# ── Non-NG options entry ──────────────────────────────────────────────────────

def _mock_selection(ltp: float = 50.0, lot_size: int = 50) -> MagicMock:
    inst = MagicMock()
    inst.lot_size = lot_size
    inst.exchange = "NFO"
    inst.tradingsymbol = "NIFTY25APR19500CE"
    sel = MagicMock()
    sel.instrument = inst
    sel.option_ltp = ltp
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
        _process_alert(alert_id, _make_alert(Action.BUY, tv_ticker="NIFTY"), s)

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
        _process_alert(alert_id, _make_alert(Action.SELL, tv_ticker="NIFTY"), s)

    assert mock_ss.call_args.args[2] == "PE"


def test_non_ng_sizing_math():
    """qty = floor(CAPITAL / option_ltp / lot_size) * lot_size."""
    # CAPITAL=10_000, ltp=50, lot_size=50 → floor(10000/50/50)*50 = 4*50 = 200
    factory = _make_factory()
    s = _s(DRY_RUN=False, CAPITAL_PER_TRADE=10_000.0)

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
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert(Action.BUY), s)

    with factory() as session:
        order = session.query(Order).filter_by(alert_id=alert_id).first()
    assert order.quantity == 200


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
        _process_alert(alert_id, _make_alert(Action.BUY), s)

    mock_ss.assert_not_called()
    mock_pe.assert_not_called()


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
        _process_alert(alert_id, _make_alert(Action.EXIT, tv_ticker="NIFTY"), s)

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
        _process_alert(alert_id, _make_alert(Action.EXIT, tv_ticker="NIFTY"), s)

    mock_cg.assert_not_called()
    mock_so.assert_not_called()


# ── TRAIL branch ──────────────────────────────────────────────────────────────

def test_trail_breakeven_trigger():
    """At 1.0R above entry, current_sl must move to entry_premium."""
    # entry=100, risk=30, breakeven trigger = 100+1.0*30=130 → new_sl=100
    factory = _make_factory()
    s = _s(DRY_RUN=True, SL_PREMIUM_PCT=0.30, BREAKEVEN_RR=1.0, TRAIL_RR=1.5)

    with factory() as session:
        _seed_instrument(session, name="NIFTY", exchange="NFO", itype="CE",
                         lot_size=1, tradingsymbol="NIFTY")
        _seed_open_position(session, tradingsymbol="NIFTY",
                            entry_premium=100.0, current_sl=70.0, ikey_suffix="be")
        alert = _seed_alert(session, tv_ticker="NIFTY", action="TRAIL", suffix="be")
        alert_id = alert.id
        session.commit()

    with patch("app.main.modify_gtt") as mock_mg:
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert(Action.TRAIL, tv_ticker="NIFTY", entry_price=130.0), s)

    with factory() as session:
        pos = session.query(Position).first()
    assert pos.current_sl == pytest.approx(100.0)
    assert pos.breakeven_moved is True
    assert pos.trail_active is False
    mock_mg.assert_not_called()   # DRY_RUN


def test_trail_trail_trigger():
    """At 1.5R above entry, trail activates and SL = current - 0.5R."""
    # entry=100, risk=30, trail trigger=145 → new_sl=145-15=130
    factory = _make_factory()
    s = _s(DRY_RUN=True, SL_PREMIUM_PCT=0.30, TRAIL_RR=1.5, TRAIL_DISTANCE_RR=0.5)

    with factory() as session:
        _seed_instrument(session, name="NIFTY", exchange="NFO", itype="CE",
                         lot_size=1, tradingsymbol="NIFTY")
        _seed_open_position(session, tradingsymbol="NIFTY",
                            entry_premium=100.0, current_sl=70.0, ikey_suffix="tr")
        alert = _seed_alert(session, tv_ticker="NIFTY", action="TRAIL", suffix="tr")
        alert_id = alert.id
        session.commit()

    with patch("app.main.modify_gtt"):
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert(Action.TRAIL, tv_ticker="NIFTY", entry_price=145.0), s)

    with factory() as session:
        pos = session.query(Position).first()
    assert pos.current_sl == pytest.approx(130.0)   # 145 - 0.5*30
    assert pos.trail_active is True
    assert pos.breakeven_moved is True


def test_trail_modify_gtt_called_with_correct_sl():
    """DRY_RUN=False: modify_gtt called with the computed new SL at breakeven."""
    # entry=100, risk=30, current=130 >= 130 → breakeven → new_sl=100
    factory = _make_factory()
    s = _s(DRY_RUN=False, SL_PREMIUM_PCT=0.30, BREAKEVEN_RR=1.0)

    with factory() as session:
        _seed_instrument(session, name="NIFTY", exchange="NFO", itype="CE",
                         lot_size=1, tradingsymbol="NIFTY")
        _seed_open_position(session, tradingsymbol="NIFTY",
                            entry_premium=100.0, current_sl=70.0, ikey_suffix="mg")
        alert = _seed_alert(session, tv_ticker="NIFTY", action="TRAIL", suffix="mg")
        alert_id = alert.id
        session.commit()

    mock_kite = MagicMock()
    with (
        patch("app.main.modify_gtt") as mock_mg,
        patch("app.main.get_session_manager") as mock_sm,
    ):
        mock_sm.return_value.get_kite.return_value = mock_kite
        main_module._SessionFactory = factory
        _process_alert(alert_id, _make_alert(Action.TRAIL, tv_ticker="NIFTY", entry_price=130.0), s)

    mock_mg.assert_called_once()
    kw = mock_mg.call_args.kwargs
    assert kw["sl_trigger"] == pytest.approx(100.0)
    assert kw["sl_limit"] == pytest.approx(100.0)


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
    """Futures fill: sl = fill_price - sl_distance, target = fill_price + target_distance."""
    factory = _make_factory()
    s = _s(DRY_RUN=True)

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
        "sl_distance": 5.0,
        "target_distance": 10.0,
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
    assert pos.current_sl == pytest.approx(195.0)   # 200 - 5
    mock_gtt.assert_not_called()   # DRY_RUN=True


# ── /kite/callback wires watcher.start() ─────────────────────────────────────

def test_kite_callback_starts_watcher():
    """Successful /kite/callback must call _watcher.start(api_key, access_token)."""
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
    mock_watcher_instance.start.assert_called_once_with(
        api_key="testkey", access_token="acc_token_xyz"
    )
