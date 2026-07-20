"""Tests for app/partial_booking.py — bank part of a winner, re-arm the rest."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import IST, Settings
from app.partial_booking import (
    book_partial,
    partial_booking_job,
    progress_to_target,
    qty_to_book,
)
from app.storage import Base, ClosedTrade, Gtt, Instrument, Order, Position

NOW = datetime(2026, 7, 20, 21, 30, tzinfo=IST)
_EXPIRY = datetime(2026, 7, 24).date()


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        KITE_API_KEY="fake",
        SECRET_KEY="",
        DASHBOARD_PASSWORD="",
        PBKDF2_ITERATIONS=1,
        DRY_RUN=True,
        PARTIAL_BOOKING_ENABLED=True,
        PARTIAL_BOOK_TRIGGER_PCT=0.60,
        PARTIAL_BOOK_QTY_PCT=0.50,
        PARTIAL_BOOK_MOVE_SL_TO_BREAKEVEN=True,
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


def _seed_position(session, *, entry_side="SELL", entry=100.0, sl=150.0, target=50.0,
                    qty=4, dry_run=True, exchange="MCX", underlying="CRUDEOILM",
                    sym="CRUDEOILM26JUL5300CE"):
    from app.storage import Alert

    alert = Alert(received_at=NOW, strategy_id="sched_straddle_CRUDEOILM",
                  tv_ticker=underlying, tv_exchange=exchange, action="SELL",
                  product="NRML", idempotency_key=f"pb-{sym}", raw_payload="{}")
    session.add(alert)
    session.flush()
    order = Order(alert_id=alert.id, kite_order_id="K1", variety="regular",
                  exchange=exchange, tradingsymbol=sym, transaction_type=entry_side,
                  order_type="MARKET", product="NRML", quantity=qty, status="COMPLETE",
                  placed_at=NOW, updated_at=NOW, dry_run=dry_run)
    session.add(order)
    session.flush()
    gtt = Gtt(order_id=order.id, kite_gtt_id=None if dry_run else 555,
              gtt_type="OCO", exchange=exchange, tradingsymbol=sym,
              sl_trigger=sl, target_trigger=target, sl_order_price=sl,
              target_order_price=target, last_price_at_placement=entry,
              status="DRY_RUN" if dry_run else "ACTIVE", placed_at=NOW,
              updated_at=NOW, dry_run=dry_run)
    session.add(gtt)
    session.flush()
    instrument = Instrument(instrument_token=777, exchange_token=777,
                            tradingsymbol=sym, name=underlying, expiry=_EXPIRY,
                            strike=5300.0, tick_size=0.1, lot_size=1,
                            instrument_type="CE", segment="MCX-OPT", exchange=exchange)
    session.add(instrument)
    position = Position(order_id=order.id, gtt_id=gtt.id, exchange=exchange,
                        tradingsymbol=sym, underlying=underlying, instrument_type="CE",
                        entry_premium=entry, current_sl=sl, quantity=qty, lot_size=1,
                        opened_at=NOW, last_updated_at=NOW)
    session.add(position)
    session.commit()
    return position, order, gtt, instrument


# ── progress_to_target ────────────────────────────────────────────────────────

def test_progress_to_target_short_halfway():
    # entry 100, target 50 (short: profit as premium falls); at 75 → 50% there
    assert progress_to_target("SELL", 100.0, 50.0, 75.0) == pytest.approx(0.5)


def test_progress_to_target_short_underwater_is_negative():
    assert progress_to_target("SELL", 100.0, 50.0, 120.0) < 0


def test_progress_to_target_long_halfway():
    # entry 100, target 200 (long: profit as premium rises); at 150 → 50%
    assert progress_to_target("BUY", 100.0, 200.0, 150.0) == pytest.approx(0.5)


def test_progress_to_target_zero_span_is_zero():
    assert progress_to_target("SELL", 100.0, 100.0, 90.0) == 0.0


# ── qty_to_book ────────────────────────────────────────────────────────────────

def test_qty_to_book_even_split():
    assert qty_to_book(4, 0.5) == 2


def test_qty_to_book_leaves_at_least_one():
    assert qty_to_book(1, 0.5) == 0   # can't split a single lot
    assert qty_to_book(2, 0.9) == 1   # never books the whole position


def test_qty_to_book_rounds_down_and_floors_at_one():
    assert qty_to_book(3, 0.5) == 1   # int(3*0.5)=1


# ── book_partial: paper mode ──────────────────────────────────────────────────

def test_book_partial_paper_records_pnl_and_resizes_remainder():
    factory = _factory()
    settings = _settings()
    with factory() as s:
        position, order, gtt, instrument = _seed_position(s, dry_run=True, qty=4)
        ok = book_partial(
            s, settings, position, order, gtt, instrument,
            ltp=75.0, now=NOW, kite=None, paper=True,
        )
        s.commit()
        assert ok is True
        assert position.quantity == 2                 # half of 4 booked
        assert position.partial_booked_qty == 2
        # SELL: profit as premium falls; CRUDEOILM MCX_LOT_UNITS=10 per Kite qty unit
        assert position.partial_booked_pnl == pytest.approx((100.0 - 75.0) * 2 * 10)
        assert position.partial_booked_at == NOW
        assert position.current_sl == pytest.approx(100.0)   # breakeven = entry
        assert gtt.sl_trigger == pytest.approx(100.0)


def test_book_partial_second_call_is_a_noop_via_eligibility_filter():
    """book_partial itself doesn't re-check the latch — the job's eligibility
    query does (partial_booked_qty == 0) — so verify that filter directly."""
    from app.partial_booking import _eligible_rows

    factory = _factory()
    with factory() as s:
        position, order, gtt, instrument = _seed_position(s, dry_run=True, qty=4)
        position.partial_booked_qty = 2
        s.commit()
        rows = _eligible_rows(s, paper=True)
    assert rows == []


def test_book_partial_skips_when_breakeven_band_does_not_bracket_ltp():
    """If breakeven SL would sit on the wrong side of a already-past LTP, the
    resize is rejected rather than shipping a broken band."""
    factory = _factory()
    settings = _settings()
    with factory() as s:
        # entry 100, target 50, but LTP already at 60 — breakeven SL of 100
        # still brackets [50, 100) around 60, so use an LTP outside any valid band
        position, order, gtt, instrument = _seed_position(s, dry_run=True, qty=4,
                                                            entry=100.0, sl=150.0, target=50.0)
        ok = book_partial(
            s, settings, position, order, gtt, instrument,
            ltp=40.0,  # below target — band [50,100) doesn't bracket 40
            now=NOW, kite=None, paper=True,
        )
    assert ok is False
    assert position.partial_booked_qty == 0   # nothing changed


def test_book_partial_single_lot_position_cannot_split():
    factory = _factory()
    settings = _settings()
    with factory() as s:
        position, order, gtt, instrument = _seed_position(s, dry_run=True, qty=1)
        ok = book_partial(
            s, settings, position, order, gtt, instrument,
            ltp=75.0, now=NOW, kite=None, paper=True,
        )
    assert ok is False


# ── book_partial: live mode ────────────────────────────────────────────────────

def test_book_partial_live_modifies_gtt_and_places_reducing_order():
    factory = _factory()
    settings = _settings()
    kite = MagicMock()
    with factory() as s:
        position, order, gtt, instrument = _seed_position(s, dry_run=False, qty=4)
        with patch("app.orders.modify_gtt") as mock_modify, \
             patch("app.orders.place_entry", return_value="EXIT123") as mock_place:
            ok = book_partial(
                s, settings, position, order, gtt, instrument,
                ltp=75.0, now=NOW, kite=kite, paper=False,
            )
        s.commit()
        assert ok is True
        mock_modify.assert_called_once()
        # GTT resized to the remaining qty, not the booked slice
        assert mock_modify.call_args.kwargs["qty"] == 2
        mock_place.assert_called_once()
        # place_entry(kite, instrument, side, qty, order_type, product)
        assert mock_place.call_args[0][2] == "BUY"          # SELL entry → BUY to reduce
        assert mock_place.call_args[0][3] == 2               # book_qty


def test_book_partial_live_restores_full_gtt_if_reducing_order_fails():
    factory = _factory()
    settings = _settings()
    kite = MagicMock()
    with factory() as s:
        position, order, gtt, instrument = _seed_position(s, dry_run=False, qty=4)
        with patch("app.orders.modify_gtt") as mock_modify, \
             patch("app.orders.place_entry", side_effect=Exception("rejected")):
            ok = book_partial(
                s, settings, position, order, gtt, instrument,
                ltp=75.0, now=NOW, kite=kite, paper=False,
            )
        assert ok is False
        assert position.quantity == 4        # untouched
        assert position.partial_booked_qty == 0
        # modify_gtt called twice: resize down, then restore to full size
        assert mock_modify.call_count == 2
        assert mock_modify.call_args.kwargs["qty"] == 4


def test_book_partial_live_skips_gtt_modify_when_no_kite_gtt_id():
    """A position whose GTT never made it to the broker (kite_gtt_id=None,
    e.g. a paper-mode row mislabeled dry) must not touch the broker at all."""
    factory = _factory()
    settings = _settings()
    with factory() as s:
        position, order, gtt, instrument = _seed_position(s, dry_run=False, qty=4)
        gtt.kite_gtt_id = None
        s.commit()
        with patch("app.orders.modify_gtt") as mock_modify, \
             patch("app.orders.place_entry", return_value="EXIT123"):
            ok = book_partial(
                s, settings, position, order, gtt, instrument,
                ltp=75.0, now=NOW, kite=MagicMock(), paper=False,
            )
        assert ok is True
        mock_modify.assert_not_called()


# ── partial_booking_job ────────────────────────────────────────────────────────

def test_job_noop_when_disabled():
    factory = _factory()
    settings = _settings(PARTIAL_BOOKING_ENABLED=False)
    with factory() as s:
        _seed_position(s, dry_run=True, qty=4)
    with patch("app.kite_session.get_session_manager") as mgr:
        partial_booking_job(settings, factory)
        mgr.assert_not_called()


def test_job_books_eligible_position_end_to_end():
    factory = _factory()
    settings = _settings()
    with factory() as s:
        _seed_position(s, dry_run=True, qty=4, entry=100.0, sl=150.0, target=50.0,
                       sym="CRUDEOILM26JUL5300CE")

    kite = MagicMock()
    # entry 100 → target 50; LTP 65 = 70% of the way there, clears the 60% trigger
    kite.ltp.return_value = {"MCX:CRUDEOILM26JUL5300CE": {"last_price": 65.0}}
    mgr = MagicMock()
    mgr.get_kite.return_value = kite
    with patch("app.partial_booking.get_session_manager", return_value=mgr):
        partial_booking_job(settings, factory)

    with factory() as s:
        pos = s.query(Position).one()
        assert pos.partial_booked_qty == 2
        assert pos.quantity == 2


def test_job_ignores_position_below_trigger():
    factory = _factory()
    settings = _settings(PARTIAL_BOOK_TRIGGER_PCT=0.60)
    with factory() as s:
        _seed_position(s, dry_run=True, qty=4, entry=100.0, sl=150.0, target=50.0,
                       sym="CRUDEOILM26JUL5300CE")

    kite = MagicMock()
    # only 20% of the way to target — below the 60% trigger
    kite.ltp.return_value = {"MCX:CRUDEOILM26JUL5300CE": {"last_price": 90.0}}
    mgr = MagicMock()
    mgr.get_kite.return_value = kite
    with patch("app.partial_booking.get_session_manager", return_value=mgr):
        partial_booking_job(settings, factory)

    with factory() as s:
        pos = s.query(Position).one()
        assert pos.partial_booked_qty == 0
        assert pos.quantity == 4


def test_job_respects_paper_live_isolation():
    """A live position must never be booked while the app is in paper mode,
    and vice versa — same isolation rule as every other job in this codebase."""
    factory = _factory()
    settings = _settings(DRY_RUN=True)   # app in paper mode
    with factory() as s:
        _seed_position(s, dry_run=False, qty=4, entry=100.0, sl=150.0, target=50.0,
                       sym="CRUDEOILM26JUL5300CE")   # but this position is live

    kite = MagicMock()
    # past the trigger, so the only reason it shouldn't book is the mode mismatch
    kite.ltp.return_value = {"MCX:CRUDEOILM26JUL5300CE": {"last_price": 65.0}}
    mgr = MagicMock()
    mgr.get_kite.return_value = kite
    with patch("app.partial_booking.get_session_manager", return_value=mgr):
        partial_booking_job(settings, factory)

    with factory() as s:
        pos = s.query(Position).one()
        assert pos.partial_booked_qty == 0   # untouched — mode mismatch
