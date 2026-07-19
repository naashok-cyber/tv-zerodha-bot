"""Tests for app/paper_trading.py — simulated GTT exits, EOD close, paper/live isolation."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import IST, Settings
from app.paper_trading import (
    _eod_exit_reason,
    is_paper_order_id,
    new_paper_order_id,
    paper_monitor_job,
    paper_pnl,
)
from app.storage import Alert, Base, ClosedTrade, Gtt, Order, Position


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        DRY_RUN=True,
        WEBHOOK_SECRET="sec",
        KITE_API_KEY="k",
        DATABASE_URL="sqlite:///:memory:",
        SECRET_KEY="",
        PBKDF2_ITERATIONS=1,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_position(
    session,
    sym: str = "NATURALGAS25JUL200CE",
    underlying: str = "NATURALGAS",
    entry_side: str = "SELL",
    entry: float = 10.0,
    sl: float = 13.0,
    target: float = 5.0,
    qty: int = 1,
    dry_run: bool = True,
    straddle_id: str | None = None,
    strategy_id: str = "sched_straddle_NATURALGAS",
    exchange: str = "MCX",
):
    now = datetime.now(IST)
    alert = Alert(
        received_at=now, strategy_id=strategy_id, tv_ticker=underlying,
        tv_exchange=exchange, action="SELL", product="NRML",
        idempotency_key=f"pt-{sym}-{entry_side}", raw_payload="{}",
    )
    session.add(alert)
    session.flush()
    order = Order(
        alert_id=alert.id, kite_order_id=new_paper_order_id() if dry_run else "123",
        variety="regular", exchange=exchange, tradingsymbol=sym,
        transaction_type=entry_side, order_type="MARKET", product="NRML",
        quantity=qty, status="COMPLETE", placed_at=now, updated_at=now,
        dry_run=dry_run, straddle_id=straddle_id,
    )
    session.add(order)
    session.flush()
    gtt = Gtt(
        order_id=order.id, gtt_type="OCO", exchange=exchange, tradingsymbol=sym,
        sl_trigger=sl, target_trigger=target, sl_order_price=sl,
        target_order_price=target, last_price_at_placement=entry,
        status="DRY_RUN" if dry_run else "ACTIVE",
        placed_at=now, updated_at=now, dry_run=dry_run,
    )
    session.add(gtt)
    session.flush()
    position = Position(
        order_id=order.id, gtt_id=gtt.id, exchange=exchange, tradingsymbol=sym,
        underlying=underlying, instrument_type="CE", entry_premium=entry,
        current_sl=sl, quantity=qty, lot_size=1, opened_at=now, last_updated_at=now,
    )
    session.add(position)
    session.flush()
    return position, order, gtt


def _run_monitor(factory, settings, ltp_map: dict):
    kite = MagicMock()
    kite.ltp.return_value = {k: {"last_price": v} for k, v in ltp_map.items()}
    mgr = MagicMock()
    mgr.get_kite.return_value = kite
    with patch("app.paper_trading.get_session_manager", return_value=mgr):
        paper_monitor_job(settings, factory)
    return kite


def test_paper_order_id_shape():
    oid = new_paper_order_id()
    assert is_paper_order_id(oid)
    assert not is_paper_order_id("230419001234")
    assert not is_paper_order_id(None)


def test_paper_pnl_mcx_units_short():
    s = _settings()
    # SELL 1 lot NG at 10, exit 13 → loss of 3 × 1 × 1250
    assert paper_pnl("SELL", 10.0, 13.0, 1, "MCX", "NATURALGAS", s) == -3750.0
    # BUY side gains when premium rises
    assert paper_pnl("BUY", 10.0, 13.0, 1, "MCX", "NATURALGAS", s) == 3750.0
    # NSE: no unit multiplier
    assert paper_pnl("BUY", 100.0, 110.0, 75, "NFO", "NIFTY", s) == 750.0


def test_monitor_closes_short_on_sl_breach():
    factory = _factory()
    s = _settings()
    with factory() as session:
        pos, _, _ = _seed_position(session, entry=10.0, sl=13.0, target=5.0)
        pos_id = pos.id
        session.commit()

    _run_monitor(factory, s, {"MCX:NATURALGAS25JUL200CE": 13.5})

    with factory() as session:
        ct = session.query(ClosedTrade).filter_by(position_id=pos_id).one()
        assert ct.exit_reason == "SL_HIT"
        assert ct.dry_run is True
        assert ct.strategy_id == "sched_straddle_NATURALGAS"
        assert ct.exit_premium == 13.5
        assert ct.pnl == -3.5 * 1250            # short leg, 1 lot NG
        gtt = session.query(Gtt).one()
        assert gtt.status == "TRIGGERED"


def test_monitor_closes_short_on_target():
    factory = _factory()
    s = _settings()
    with factory() as session:
        pos, _, _ = _seed_position(session, entry=10.0, sl=13.0, target=5.0)
        pos_id = pos.id
        session.commit()

    _run_monitor(factory, s, {"MCX:NATURALGAS25JUL200CE": 4.8})

    with factory() as session:
        ct = session.query(ClosedTrade).filter_by(position_id=pos_id).one()
        assert ct.exit_reason == "TARGET_HIT"
        assert ct.pnl == (10.0 - 4.8) * 1250


def test_monitor_no_exit_inside_band():
    factory = _factory()
    s = _settings()
    with factory() as session:
        _seed_position(session, entry=10.0, sl=13.0, target=5.0)
        session.commit()

    _run_monitor(factory, s, {"MCX:NATURALGAS25JUL200CE": 9.0})

    with factory() as session:
        assert session.query(ClosedTrade).count() == 0


def test_monitor_ignores_live_positions():
    factory = _factory()
    s = _settings()
    with factory() as session:
        _seed_position(session, dry_run=False, entry=10.0, sl=13.0, target=5.0)
        session.commit()

    kite = _run_monitor(factory, s, {"MCX:NATURALGAS25JUL200CE": 20.0})

    kite.ltp.assert_not_called()   # no paper rows → early return
    with factory() as session:
        assert session.query(ClosedTrade).count() == 0


def test_monitor_paired_straddle_exit():
    factory = _factory()
    s = _settings()
    with factory() as session:
        ce, _, _ = _seed_position(
            session, sym="NG25JUL200CE", entry=10.0, sl=13.0, target=5.0,
            straddle_id="st-1",
        )
        pe, _, _ = _seed_position(
            session, sym="NG25JUL200PE", entry=9.0, sl=12.0, target=4.0,
            straddle_id="st-1",
        )
        ce_id, pe_id = ce.id, pe.id
        session.commit()

    # CE breaches SL; PE is inside its band but must be closed as the pair.
    _run_monitor(factory, s, {
        "MCX:NG25JUL200CE": 13.2,
        "MCX:NG25JUL200PE": 6.0,
    })

    with factory() as session:
        ce_ct = session.query(ClosedTrade).filter_by(position_id=ce_id).one()
        pe_ct = session.query(ClosedTrade).filter_by(position_id=pe_id).one()
        assert ce_ct.exit_reason == "SL_HIT"
        assert pe_ct.exit_reason == "straddle_paired_sl_exit"
        assert pe_ct.exit_premium == 6.0
        assert pe_ct.pnl == (9.0 - 6.0) * 1250


def test_eod_exit_reason_routing():
    s = _settings()
    now_eve = datetime.now(IST).replace(hour=23, minute=26)
    now_noon = datetime.now(IST).replace(hour=12, minute=0)
    pos = MagicMock(exchange="MCX")
    assert _eod_exit_reason(pos, None, now_eve, s) == "EOD_MCX"
    assert _eod_exit_reason(pos, None, now_noon, s) is None
    # scheduled straddles close at STRADDLE_SQUAREOFF_TIME (23:20 default)
    now_2321 = datetime.now(IST).replace(hour=23, minute=21)
    assert _eod_exit_reason(pos, "sched_straddle_NATURALGAS", now_2321, s) == (
        "scheduled_straddle_squareoff"
    )
    nse_pos = MagicMock(exchange="NFO")
    now_1526 = datetime.now(IST).replace(hour=15, minute=26)
    assert _eod_exit_reason(nse_pos, None, now_1526, s) == "EOD_NSE"


def test_eod_squareoff_job_skips_paper_positions():
    """The real EOD job must never square off simulated positions at Kite."""
    from app.scheduler import eod_squareoff_job

    factory = _factory()
    with factory() as session:
        _seed_position(session, dry_run=True)
        session.commit()

    kite = MagicMock()
    mgr = MagicMock()
    mgr.get_kite.return_value = kite
    with patch("app.scheduler.get_session_manager", return_value=mgr):
        eod_squareoff_job(
            session_factory=factory,
            exchanges=["MCX", "MCX-OPT"],
            reason="EOD_MCX",
            dry_run=False,
        )

    kite.place_order.assert_not_called()
    with factory() as session:
        assert session.query(ClosedTrade).count() == 0
        gtt = session.query(Gtt).one()
        assert gtt.status == "DRY_RUN"   # untouched
