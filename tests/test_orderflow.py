"""Tests for app/orderflow.py — quote-rule classification, binning, flush, read side."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import IST, Settings
from app.orderflow import (
    SIDE_BUY,
    SIDE_SELL,
    SIDE_SPLIT,
    OrderFlowRecorder,
    _bucket,
    classify,
    load_footprint,
    orderflow_prune_job,
    session_stats,
    stacked_levels,
)
from app.storage import Base, Instrument, OrderFlowBar, OrderFlowCell


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        DRY_RUN=True,
        WEBHOOK_SECRET="sec",
        KITE_API_KEY="k",
        DATABASE_URL="sqlite:///:memory:",
        SECRET_KEY="",
        PBKDF2_ITERATIONS=1,
        ORDERFLOW_ENABLED=True,
        ORDERFLOW_SYMBOLS=["NATURALGAS"],
        ORDERFLOW_BAR_SECONDS=60,
        ORDERFLOW_ROW_TICKS=2,      # NG tick 0.10 -> 0.20 rows
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


def _tick(token=111, ltp=250.0, vol=1000.0, bid=249.9, ask=250.1, ts=None) -> dict:
    t = {
        "instrument_token": token,
        "last_price": ltp,
        "volume_traded": vol,
        "exchange_timestamp": ts or datetime(2026, 8, 21, 10, 30, 5, tzinfo=IST),
    }
    if bid is not None or ask is not None:
        t["depth"] = {
            "buy": [{"price": bid, "quantity": 5}] if bid is not None else [],
            "sell": [{"price": ask, "quantity": 5}] if ask is not None else [],
        }
    return t


def _seed_instrument(session, token=111, name="NATURALGAS", tick=0.10):
    session.add(Instrument(
        instrument_token=token, exchange_token=token, tradingsymbol=f"{name}25SEPFUT",
        name=name, expiry=date.today() + timedelta(days=20), strike=None,
        tick_size=tick, lot_size=1250, instrument_type="FUT",
        segment="MCX-FUT", exchange="MCX",
    ))
    session.commit()


# ── classification ────────────────────────────────────────────────────────────

def test_classify_lifting_the_offer_is_a_buy():
    assert classify(250.1, 249.9, 250.1, None, 1) == (SIDE_BUY, 1)
    assert classify(250.5, 249.9, 250.1, None, 1) == (SIDE_BUY, 1)


def test_classify_hitting_the_bid_is_a_sell():
    assert classify(249.9, 249.9, 250.1, None, 1) == (SIDE_SELL, -1)
    assert classify(249.5, 249.9, 250.1, None, 1) == (SIDE_SELL, -1)


def test_classify_inside_spread_uses_midpoint():
    # book 249.9 / 250.3, mid 250.1
    assert classify(250.2, 249.9, 250.3, None, 1)[0] == SIDE_BUY
    assert classify(250.0, 249.9, 250.3, None, 1)[0] == SIDE_SELL


def test_classify_falls_back_to_tick_rule_without_a_book():
    assert classify(250.0, None, None, 249.5, 1)[0] == SIDE_BUY
    assert classify(250.0, None, None, 250.5, 1)[0] == SIDE_SELL
    # unchanged price carries the previous direction
    assert classify(250.0, None, None, 250.0, -1) == (SIDE_SELL, -1)


def test_classify_splits_when_nothing_is_known():
    assert classify(250.0, None, None, None, 1)[0] == SIDE_SPLIT


def test_bucket_floors_to_bar_start():
    ts = datetime(2026, 8, 21, 10, 30, 47, tzinfo=IST)
    assert _bucket(ts, 60) == datetime(2026, 8, 21, 10, 30, 0, tzinfo=IST)
    assert _bucket(ts, 300) == datetime(2026, 8, 21, 10, 30, 0, tzinfo=IST)
    assert _bucket(datetime(2026, 8, 21, 10, 33, 5, tzinfo=IST), 300) == \
        datetime(2026, 8, 21, 10, 30, 0, tzinfo=IST)


# ── aggregation ───────────────────────────────────────────────────────────────

def _recorder(factory, **kw):
    rec = OrderFlowRecorder(session_factory=factory, settings=_settings(**kw))
    with factory() as s:
        _seed_instrument(s)
        rec.resolve_tokens(s)
    return rec


def test_first_tick_only_sets_the_volume_baseline():
    factory = _factory()
    rec = _recorder(factory)
    rec.on_ticks([_tick(vol=1000.0)])
    # No prior cumulative volume, so nothing can be attributed yet.
    assert rec.flush(force=True) == 0


def test_volume_delta_is_classified_and_binned():
    factory = _factory()
    rec = _recorder(factory)
    rec.on_ticks([_tick(ltp=250.0, vol=1000.0)])
    rec.on_ticks([_tick(ltp=250.1, vol=1030.0)])   # +30 at the ask -> buy
    rec.on_ticks([_tick(ltp=249.9, vol=1050.0)])   # +20 at the bid -> sell
    assert rec.flush(force=True) == 1

    with factory() as s:
        bar = s.query(OrderFlowBar).one()
        assert bar.buy_qty == 30
        assert bar.sell_qty == 20
        assert bar.delta == 10
        assert bar.max_delta == 30      # delta path peaked before the sell
        assert bar.min_delta == 0
        assert bar.high == 250.1 and bar.low == 249.9
        cells = s.query(OrderFlowCell).all()
        assert {round(c.price, 2) for c in cells} == {250.0, 249.8}


def test_negative_volume_delta_is_ignored_as_a_day_rollover():
    factory = _factory()
    rec = _recorder(factory)
    rec.on_ticks([_tick(vol=5000.0)])
    rec.on_ticks([_tick(vol=10.0)])     # new session, counter reset
    assert rec.flush(force=True) == 0


def test_bars_split_on_the_bar_boundary():
    factory = _factory()
    rec = _recorder(factory)
    t0 = datetime(2026, 8, 21, 10, 30, 5, tzinfo=IST)
    rec.on_ticks([_tick(vol=1000.0, ts=t0)])
    rec.on_ticks([_tick(ltp=250.1, vol=1010.0, ts=t0 + timedelta(seconds=20))])
    rec.on_ticks([_tick(ltp=250.1, vol=1020.0, ts=t0 + timedelta(seconds=80))])
    assert rec.flush(force=True) == 2


def test_in_progress_bar_is_not_flushed():
    factory = _factory()
    rec = _recorder(factory)
    t0 = datetime(2026, 8, 21, 10, 30, 5, tzinfo=IST)
    rec.on_ticks([_tick(vol=1000.0, ts=t0)])
    rec.on_ticks([_tick(ltp=250.1, vol=1010.0, ts=t0)])
    # "now" still inside the 10:30 bar
    assert rec.flush(now=datetime(2026, 8, 21, 10, 30, 40, tzinfo=IST)) == 0
    assert rec.flush(now=datetime(2026, 8, 21, 10, 31, 1, tzinfo=IST)) == 1


def test_disabled_recorder_ignores_ticks():
    factory = _factory()
    rec = OrderFlowRecorder(
        session_factory=factory,
        settings=_settings(),
        enabled_fn=lambda: False,
    )
    with factory() as s:
        _seed_instrument(s)
        rec.resolve_tokens(s)
    rec.on_ticks([_tick(vol=1000.0)])
    rec.on_ticks([_tick(ltp=250.1, vol=1030.0)])
    assert rec.flush(force=True) == 0


def test_untracked_tokens_are_skipped():
    factory = _factory()
    rec = _recorder(factory)
    rec.on_ticks([_tick(token=999, vol=1000.0)])
    rec.on_ticks([_tick(token=999, ltp=250.1, vol=1030.0)])
    assert rec.flush(force=True) == 0


def test_on_ticks_never_raises_on_malformed_input():
    """The WS thread also drives trailing SL — a footprint bug must not kill it."""
    factory = _factory()
    rec = _recorder(factory)
    rec.on_ticks([{"instrument_token": 111}])            # no price
    rec.on_ticks([{"instrument_token": 111, "last_price": None}])
    rec.on_ticks([{"last_price": 250.0}])                # no token
    rec.on_ticks([_tick(ltp=0)])
    assert rec.flush(force=True) == 0


def test_resolve_tokens_keeps_the_volume_cursor():
    factory = _factory()
    rec = _recorder(factory)
    rec.on_ticks([_tick(vol=1000.0)])
    with factory() as s:
        rec.resolve_tokens(s)                    # mid-session re-resolve
    rec.on_ticks([_tick(ltp=250.1, vol=1030.0)])
    assert rec.flush(force=True) == 1
    with factory() as s:
        assert s.query(OrderFlowBar).one().buy_qty == 30


# ── read side ─────────────────────────────────────────────────────────────────

def _seed_bar(session, ts, cells: list[tuple[float, float, float]], row_size=0.2):
    """cells = [(price, bid, ask), ...]"""
    buy = sum(c[2] for c in cells)
    sell = sum(c[1] for c in cells)
    bar = OrderFlowBar(
        instrument_token=111, tradingsymbol="NATURALGAS25SEPFUT",
        underlying="NATURALGAS", exchange="MCX", bucket_ts=ts, bar_seconds=60,
        row_size=row_size, open=cells[0][0], high=max(c[0] for c in cells),
        low=min(c[0] for c in cells), close=cells[-1][0],
        buy_qty=buy, sell_qty=sell, max_delta=buy, min_delta=0.0,
        poc_price=cells[0][0], tick_count=len(cells),
    )
    bar.cells = [OrderFlowCell(price=p, bid_qty=b, ask_qty=a) for p, b, a in cells]
    session.add(bar)
    session.commit()
    return bar


def test_load_footprint_marks_diagonal_imbalances_and_poc():
    factory = _factory()
    ts = datetime(2026, 8, 21, 10, 30, tzinfo=IST)
    with factory() as s:
        # rows 249.8 / 250.0 / 250.2; ask at 250.2 is 10x the bid one row below
        _seed_bar(s, ts, [(249.8, 100.0, 10.0), (250.0, 10.0, 20.0), (250.2, 5.0, 200.0)])
        bars = load_footprint(s, "NATURALGAS", limit=5, imbalance_ratio=3.0, min_cell_pct=1.0)

    assert len(bars) == 1
    rows = {round(r["price"], 2): r for r in bars[0]["rows"]}
    assert rows[250.2]["buy_imb"] is True      # 200 ask vs 10 bid below
    assert rows[249.8]["sell_imb"] is True     # 100 bid vs 20 ask above
    assert rows[250.2]["poc"] is True          # 205 total is the fattest row
    assert bars[0]["delta"] == 230.0 - 115.0


def test_load_footprint_min_cell_filter_kills_one_lot_imbalances():
    factory = _factory()
    ts = datetime(2026, 8, 21, 10, 30, tzinfo=IST)
    with factory() as s:
        _seed_bar(s, ts, [(249.8, 0.0, 0.0), (250.0, 0.0, 1.0), (250.2, 0.0, 900.0)])
        loose = load_footprint(s, "NATURALGAS", min_cell_pct=0.0)
        strict = load_footprint(s, "NATURALGAS", min_cell_pct=1.0)
    loose_rows = {round(r["price"], 2): r for r in loose[0]["rows"]}
    strict_rows = {round(r["price"], 2): r for r in strict[0]["rows"]}
    assert loose_rows[250.0]["buy_imb"] is True     # 1 lot against an empty row
    assert strict_rows[250.0]["buy_imb"] is False   # below 1% of bar volume
    assert strict_rows[250.2]["buy_imb"] is True    # the real one survives


def test_load_footprint_leaves_ladder_edges_unmarked():
    """The top row has no ask above it and the bottom row no bid below it — those
    are absent rows, not zero-volume ones. Marking them would flag an imbalance on
    every bar's extremes and manufacture stacks out of thin air."""
    factory = _factory()
    ts = datetime(2026, 8, 21, 10, 30, tzinfo=IST)
    with factory() as s:
        _seed_bar(s, ts, [(249.8, 50.0, 50.0), (250.0, 50.0, 50.0), (250.2, 50.0, 50.0)])
        bars = load_footprint(s, "NATURALGAS", min_cell_pct=0.0)
    rows = {round(r["price"], 2): r for r in bars[0]["rows"]}
    assert rows[249.8]["buy_imb"] is False     # nothing below to compare against
    assert rows[250.2]["sell_imb"] is False    # nothing above to compare against
    # a genuinely skipped row *inside* the range still counts as zero
    with factory() as s:
        # 250.0 never traded, so no cell was stored for it at all
        _seed_bar(s, ts + timedelta(minutes=1), [(249.8, 5.0, 5.0), (250.2, 5.0, 400.0)])
        gap = load_footprint(s, "NATURALGAS", limit=1, min_cell_pct=0.0)
    gap_rows = {round(r["price"], 2): r for r in gap[0]["rows"]}
    assert gap_rows[250.2]["buy_imb"] is True


def test_load_footprint_returns_oldest_first():
    factory = _factory()
    base = datetime(2026, 8, 21, 10, 30, tzinfo=IST)
    with factory() as s:
        for i in range(4):
            _seed_bar(s, base + timedelta(minutes=i), [(250.0, 5.0, 5.0)])
        bars = load_footprint(s, "NATURALGAS", limit=3)
    assert [b["ts"] for b in bars] == [
        base + timedelta(minutes=1), base + timedelta(minutes=2), base + timedelta(minutes=3),
    ]


def test_stacked_levels_needs_consecutive_rows():
    factory = _factory()
    ts = datetime(2026, 8, 21, 10, 30, tzinfo=IST)
    with factory() as s:
        _seed_bar(s, ts, [
            (249.8, 1.0, 100.0), (250.0, 1.0, 100.0),
            (250.2, 1.0, 100.0), (250.4, 1.0, 100.0),
        ])
        bars = load_footprint(s, "NATURALGAS", min_cell_pct=0.0)
    assert stacked_levels(bars, stack_len=3)
    assert not stacked_levels(bars, stack_len=9)


def test_session_stats_builds_the_cvd_path():
    factory = _factory()
    base = datetime.now(IST).replace(hour=10, minute=30, second=0, microsecond=0)
    with factory() as s:
        _seed_bar(s, base, [(250.0, 10.0, 30.0)])                      # +20
        _seed_bar(s, base + timedelta(minutes=1), [(250.0, 40.0, 5.0)])  # -35
        stats = session_stats(s, "NATURALGAS")
    assert stats["bars"] == 2
    assert stats["delta"] == -15.0
    assert stats["cvd"] == [20.0, -15.0]


def test_prune_drops_old_bars_and_their_cells():
    factory = _factory()
    now = datetime.now(IST)
    with factory() as s:
        _seed_bar(s, now - timedelta(days=30), [(250.0, 5.0, 5.0)])
        _seed_bar(s, now, [(250.0, 5.0, 5.0)])
    orderflow_prune_job(_settings(ORDERFLOW_RETENTION_DAYS=5), factory)
    with factory() as s:
        assert s.query(OrderFlowBar).count() == 1
        assert s.query(OrderFlowCell).count() == 1
