"""Live order-flow recorder — real bid/ask footprint from the Kite tick stream.

Why this exists
---------------
TradingView cannot give Pine a bid/ask tape, so every "order flow" indicator
there (including TradingView's own Up/Down Volume) infers the buy/sell split
from lower-timeframe candle direction. We have something better: the Kite
WebSocket already running in app/watcher.py. Full-mode ticks carry the last
price, cumulative day volume **and** 5-level depth, which lets us classify
volume with the *quote rule* — compare the traded price against the prevailing
best bid/ask — the same method commercial tools use for Indian markets, since
neither NSE nor MCX publishes an aggressor flag.

Cost model (this is why it rides the existing socket)
-----------------------------------------------------
- **Zero REST quota.** Kite's 1-req/sec quote cap applies to the REST API. The
  streaming socket is a separate channel, so nothing here competes with the
  hedge/EMA/defense crons or `app.orders.throttled_quote_call`.
- **Zero extra connections.** We reuse OrderWatcher's ticker rather than
  opening a second one (Kite allows 3 per api_key).
- **The tick callback must stay O(1).** It runs on the WebSocket thread, the
  same thread that drives trailing-SL updates — so `on_ticks` only touches
  in-memory dicts. Every DB write happens on the scheduler thread in `flush()`.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings
from app.storage import Instrument, OrderFlowBar, OrderFlowCell

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Classification outcomes, kept as constants so the UI and tests agree.
SIDE_BUY = "buy"
SIDE_SELL = "sell"
SIDE_SPLIT = "split"


# ── in-memory aggregation ─────────────────────────────────────────────────────

@dataclass
class _Cell:
    bid: float = 0.0   # aggressive selling that printed on this row
    ask: float = 0.0   # aggressive buying


@dataclass
class _Bar:
    token: int
    tradingsymbol: str
    underlying: str
    exchange: str
    bucket_ts: datetime
    bar_seconds: int
    row_size: float
    open: float
    high: float
    low: float
    close: float
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    run_delta: float = 0.0
    max_delta: float = 0.0
    min_delta: float = 0.0
    tick_count: int = 0
    cells: dict[int, _Cell] = field(default_factory=dict)


@dataclass
class _Tracked:
    token: int
    tradingsymbol: str
    underlying: str
    exchange: str
    tick_size: float
    row_size: float
    last_volume: float | None = None
    last_price: float | None = None
    last_dir: int = 1


def classify(
    ltp: float,
    best_bid: float | None,
    best_ask: float | None,
    prev_price: float | None,
    last_dir: int,
) -> tuple[str, int]:
    """Quote rule: which side was the aggressor at `ltp`?

    Returns (side, new_last_dir). `side` is buy/sell/split — split means the
    print landed exactly mid-spread with no prior price to lean on, so the
    volume is halved rather than guessed.

    Traded at or above the ask ⇒ someone lifted the offer. At or below the bid
    ⇒ someone hit the bid. Inside the spread (Kite snapshots ~1/sec, so the
    book has often moved by the time we see the print) we fall back to the
    midpoint, then to the tick rule.
    """
    if best_ask and ltp >= best_ask:
        return SIDE_BUY, 1
    if best_bid and ltp <= best_bid:
        return SIDE_SELL, -1
    if best_bid and best_ask and best_ask > best_bid:
        mid = (best_bid + best_ask) / 2.0
        if ltp > mid:
            return SIDE_BUY, 1
        if ltp < mid:
            return SIDE_SELL, -1
    # No usable book — Lee-Ready tick rule against the previous print.
    if prev_price is not None:
        if ltp > prev_price:
            return SIDE_BUY, 1
        if ltp < prev_price:
            return SIDE_SELL, -1
        return (SIDE_BUY if last_dir > 0 else SIDE_SELL), last_dir
    return SIDE_SPLIT, last_dir


def _best_levels(tick: dict) -> tuple[float | None, float | None]:
    depth = tick.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    bid = float(buys[0].get("price") or 0) if buys else 0.0
    ask = float(sells[0].get("price") or 0) if sells else 0.0
    return (bid or None), (ask or None)


def _bucket(ts: datetime, bar_seconds: int) -> datetime:
    """Floor a timestamp to the start of its bar."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    ts = ts.astimezone(IST)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % bar_seconds), tz=IST)


class OrderFlowRecorder:
    """Aggregates ticks into footprint bars in memory; flushed to SQLite by cron."""

    def __init__(
        self,
        session_factory: Any,
        settings: Settings | None = None,
        enabled_fn: Callable[[], bool] | None = None,
        symbols_fn: Callable[[], list[str]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._enabled_fn = enabled_fn
        self._symbols_fn = symbols_fn
        self._tracked: dict[int, _Tracked] = {}
        self._bars: dict[tuple[int, datetime], _Bar] = {}
        self._lock = threading.Lock()

    # ── configuration ────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        if self._enabled_fn is not None:
            try:
                return bool(self._enabled_fn())
            except Exception:
                log.exception("orderflow: enabled_fn raised; treating as disabled")
                return False
        return bool(self._settings.ORDERFLOW_ENABLED)

    def _symbols(self) -> list[str]:
        if self._symbols_fn is not None:
            try:
                return list(self._symbols_fn())
            except Exception:
                log.exception("orderflow: symbols_fn raised; falling back to .env")
        return list(self._settings.ORDERFLOW_SYMBOLS)

    @property
    def tokens(self) -> list[int]:
        with self._lock:
            return sorted(self._tracked)

    def resolve_tokens(self, session: Any) -> list[int]:
        """Map the configured underlyings to front-month futures tokens.

        Returns the full token set to subscribe. Safe to call repeatedly — it
        only rebuilds the tracking table, never touches accumulated bars.
        """
        s = self._settings
        wanted: dict[int, _Tracked] = {}
        for name in self._symbols():
            name = (name or "").strip().upper()
            if not name:
                continue
            inst = (
                session.query(Instrument)
                .filter(
                    Instrument.name == name,
                    Instrument.instrument_type == "FUT",
                    Instrument.expiry >= datetime.now(IST).date(),
                )
                .order_by(Instrument.expiry)
                .first()
            )
            if inst is None:
                log.warning("orderflow: no front-month future for %s — skipping", name)
                continue
            tick_size = float(inst.tick_size or 0.05) or 0.05
            wanted[int(inst.instrument_token)] = _Tracked(
                token=int(inst.instrument_token),
                tradingsymbol=inst.tradingsymbol,
                underlying=name,
                exchange=inst.exchange,
                tick_size=tick_size,
                row_size=tick_size * max(1, int(s.ORDERFLOW_ROW_TICKS)),
            )
        with self._lock:
            # Carry per-token cursors across a re-resolve so a mid-session
            # refresh does not restate one bar's volume as a fresh baseline.
            for token, tracked in wanted.items():
                old = self._tracked.get(token)
                if old is not None:
                    tracked.last_volume = old.last_volume
                    tracked.last_price = old.last_price
                    tracked.last_dir = old.last_dir
            self._tracked = wanted
        return sorted(wanted)

    # ── hot path (WebSocket thread) ──────────────────────────────────────────

    def on_ticks(self, ticks: list[dict]) -> None:
        """Fold a tick batch into the in-memory bars. Never blocks, never I/Os."""
        if not ticks or not self.is_enabled():
            return
        try:
            self._fold(ticks)
        except Exception:
            # A recorder bug must never take down trailing-SL tick handling.
            log.exception("orderflow: on_ticks failed")

    def _fold(self, ticks: list[dict]) -> None:
        bar_seconds = max(5, int(self._settings.ORDERFLOW_BAR_SECONDS))
        with self._lock:
            for tick in ticks:
                token = tick.get("instrument_token")
                tracked = self._tracked.get(token) if token is not None else None
                if tracked is None:
                    continue
                ltp = tick.get("last_price")
                if ltp is None:
                    continue
                ltp = float(ltp)
                if ltp <= 0:
                    continue

                ts = tick.get("exchange_timestamp") or tick.get("last_trade_time")
                if not isinstance(ts, datetime):
                    ts = datetime.now(IST)
                bucket_ts = _bucket(ts, bar_seconds)

                key = (tracked.token, bucket_ts)
                bar = self._bars.get(key)
                if bar is None:
                    bar = _Bar(
                        token=tracked.token,
                        tradingsymbol=tracked.tradingsymbol,
                        underlying=tracked.underlying,
                        exchange=tracked.exchange,
                        bucket_ts=bucket_ts,
                        bar_seconds=bar_seconds,
                        row_size=tracked.row_size,
                        open=ltp, high=ltp, low=ltp, close=ltp,
                    )
                    self._bars[key] = bar

                bar.high = max(bar.high, ltp)
                bar.low = min(bar.low, ltp)
                bar.close = ltp
                bar.tick_count += 1

                # Traded quantity since the previous snapshot. Kite streams
                # ~1/sec snapshots, not every print, so this is volume-per-
                # interval rather than the raw tape.
                vol = tick.get("volume_traded")
                dv = 0.0
                if vol is not None:
                    vol = float(vol)
                    if tracked.last_volume is not None and vol >= tracked.last_volume:
                        dv = vol - tracked.last_volume
                    tracked.last_volume = vol
                elif tick.get("last_traded_quantity") is not None:
                    # LTP/quote mode fallback: one print's worth, better than nothing.
                    dv = float(tick.get("last_traded_quantity") or 0)

                if dv > 0:
                    best_bid, best_ask = _best_levels(tick)
                    side, tracked.last_dir = classify(
                        ltp, best_bid, best_ask, tracked.last_price, tracked.last_dir,
                    )
                    if side == SIDE_BUY:
                        buy, sell = dv, 0.0
                    elif side == SIDE_SELL:
                        buy, sell = 0.0, dv
                    else:
                        buy = sell = dv / 2.0

                    row = int(math.floor(ltp / tracked.row_size + 1e-9))
                    cell = bar.cells.get(row)
                    if cell is None:
                        cell = _Cell()
                        bar.cells[row] = cell
                    cell.ask += buy
                    cell.bid += sell

                    bar.buy_qty += buy
                    bar.sell_qty += sell
                    bar.run_delta += buy - sell
                    bar.max_delta = max(bar.max_delta, bar.run_delta)
                    bar.min_delta = min(bar.min_delta, bar.run_delta)

                tracked.last_price = ltp

    # ── flush (scheduler thread) ─────────────────────────────────────────────

    def flush(self, *, force: bool = False, now: datetime | None = None) -> int:
        """Persist every completed bar. Returns the number of bars written.

        The in-progress bar stays in memory unless force=True (EOD/shutdown).
        """
        now = now or datetime.now(IST)
        with self._lock:
            ready: list[_Bar] = []
            for key, bar in list(self._bars.items()):
                closes_at = bar.bucket_ts + timedelta(seconds=bar.bar_seconds)
                if force or closes_at <= now:
                    ready.append(bar)
                    del self._bars[key]
        if not ready:
            return 0

        written = 0
        try:
            with self._session_factory() as session:
                for bar in ready:
                    if not bar.cells:
                        continue
                    poc_row = max(bar.cells.items(), key=lambda kv: kv[1].bid + kv[1].ask)[0]
                    row = OrderFlowBar(
                        instrument_token=bar.token,
                        tradingsymbol=bar.tradingsymbol,
                        underlying=bar.underlying,
                        exchange=bar.exchange,
                        bucket_ts=bar.bucket_ts,
                        bar_seconds=bar.bar_seconds,
                        row_size=bar.row_size,
                        open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                        buy_qty=bar.buy_qty, sell_qty=bar.sell_qty,
                        max_delta=bar.max_delta, min_delta=bar.min_delta,
                        poc_price=poc_row * bar.row_size,
                        tick_count=bar.tick_count,
                    )
                    row.cells = [
                        OrderFlowCell(
                            price=idx * bar.row_size,
                            bid_qty=cell.bid,
                            ask_qty=cell.ask,
                        )
                        for idx, cell in sorted(bar.cells.items())
                    ]
                    session.add(row)
                    written += 1
                session.commit()
        except Exception:
            log.exception("orderflow: flush failed — %d bar(s) dropped", len(ready))
            return 0
        if written:
            log.debug("orderflow: flushed %d bar(s)", written)
        return written


# ── module-level singleton (wired in main.py lifespan) ────────────────────────

_recorder: OrderFlowRecorder | None = None


def set_recorder(recorder: OrderFlowRecorder | None) -> None:
    global _recorder
    _recorder = recorder


def get_recorder() -> OrderFlowRecorder | None:
    return _recorder


# ── scheduled jobs ────────────────────────────────────────────────────────────

def orderflow_flush_job(
    settings: Settings,
    session_factory: Any,
    subscribe_fn: Callable[[list[int]], None] | None = None,
) -> None:
    """1-min cron: persist completed bars and keep the subscription current.

    Makes no Kite REST calls — it only writes SQLite and (when the tracked
    token set changes) asks the existing WebSocket to subscribe.
    """
    recorder = get_recorder()
    if recorder is None:
        return
    if not recorder.is_enabled():
        return
    try:
        with session_factory() as session:
            tokens = recorder.resolve_tokens(session)
    except Exception:
        log.exception("orderflow: token resolve failed")
        tokens = recorder.tokens
    if tokens and subscribe_fn is not None:
        try:
            subscribe_fn(tokens)
        except Exception:
            log.exception("orderflow: subscribe failed")
    recorder.flush()


def orderflow_prune_job(settings: Settings, session_factory: Any) -> None:
    """Nightly retention sweep — footprint data is only useful while it's fresh."""
    days = max(1, int(settings.ORDERFLOW_RETENTION_DAYS))
    cutoff = datetime.now(IST) - timedelta(days=days)
    try:
        with session_factory() as session:
            stale = (
                session.query(OrderFlowBar.id)
                .filter(OrderFlowBar.bucket_ts < cutoff)
                .all()
            )
            ids = [r[0] for r in stale]
            if not ids:
                return
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start:chunk_start + 500]
                session.query(OrderFlowCell).filter(
                    OrderFlowCell.bar_id.in_(chunk)
                ).delete(synchronize_session=False)
                session.query(OrderFlowBar).filter(
                    OrderFlowBar.id.in_(chunk)
                ).delete(synchronize_session=False)
            session.commit()
            log.info("orderflow: pruned %d bar(s) older than %d day(s)", len(ids), days)
    except Exception:
        log.exception("orderflow: prune failed")


# ── read side (used by /orderflow) ────────────────────────────────────────────

def load_footprint(
    session: Any,
    underlying: str,
    limit: int = 14,
    imbalance_ratio: float = 3.0,
    min_cell_pct: float = 1.0,
) -> list[dict]:
    """Most recent `limit` bars for one underlying, oldest first, with
    per-row diagonal imbalances and POC resolved for rendering.
    """
    bars = (
        session.query(OrderFlowBar)
        .filter(OrderFlowBar.underlying == underlying.upper())
        .order_by(OrderFlowBar.bucket_ts.desc())
        .limit(max(1, limit))
        .all()
    )
    out: list[dict] = []
    for bar in reversed(bars):
        cells = sorted(bar.cells, key=lambda c: c.price)
        if not cells:
            continue
        by_row = {
            int(round(c.price / bar.row_size)): (c.bid_qty or 0.0, c.ask_qty or 0.0)
            for c in cells
        }
        total = sum(b + a for b, a in by_row.values())
        min_cell = total * min_cell_pct / 100.0
        poc_row = max(by_row.items(), key=lambda kv: kv[1][0] + kv[1][1])[0]
        lo_row, hi_row = min(by_row), max(by_row)

        rows = []
        for idx in sorted(by_row):
            bid, ask = by_row[idx]
            # Diagonal comparison needs a neighbour row that exists inside the bar's
            # range. A row the bar never reached is a structural absence, not a zero —
            # treating it as one marks an imbalance on every bar's extremes and
            # fabricates stacks. A skipped row *within* the range really is 0 volume.
            below_bid = by_row.get(idx - 1, (0.0, 0.0))[0] if idx - 1 >= lo_row else None
            above_ask = by_row.get(idx + 1, (0.0, 0.0))[1] if idx + 1 <= hi_row else None
            buy_imb = ask >= min_cell and below_bid is not None and (
                ask > 0 if below_bid <= 0 else ask / below_bid >= imbalance_ratio
            )
            sell_imb = bid >= min_cell and above_ask is not None and (
                bid > 0 if above_ask <= 0 else bid / above_ask >= imbalance_ratio
            )
            rows.append({
                "row": idx,
                "price": idx * bar.row_size,
                "bid": bid,
                "ask": ask,
                "total": bid + ask,
                "buy_imb": bool(buy_imb),
                "sell_imb": bool(sell_imb),
                "poc": idx == poc_row,
            })
        out.append({
            "ts": bar.bucket_ts,
            "tradingsymbol": bar.tradingsymbol,
            "row_size": bar.row_size,
            "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
            "buy": bar.buy_qty, "sell": bar.sell_qty,
            "delta": (bar.buy_qty or 0.0) - (bar.sell_qty or 0.0),
            "max_delta": bar.max_delta, "min_delta": bar.min_delta,
            "volume": total,
            "poc_price": poc_row * bar.row_size,
            "rows": rows,
        })
    return out


def stacked_levels(bars: list[dict], stack_len: int = 3) -> list[dict]:
    """Runs of `stack_len` consecutive imbalanced rows within a bar.

    Same read as the Pine footprint: aggressive flow that cleared several
    price levels in sequence tends to hold on a revisit.
    """
    levels: list[dict] = []
    for bar in bars:
        for side in ("buy_imb", "sell_imb"):
            run: list[dict] = []
            for row in bar["rows"]:
                if row[side]:
                    run.append(row)
                    continue
                if len(run) >= stack_len:
                    levels.append({
                        "ts": bar["ts"],
                        "side": "buy" if side == "buy_imb" else "sell",
                        "low": run[0]["price"],
                        "high": run[-1]["price"] + bar["row_size"],
                        "rows": len(run),
                    })
                run = []
            if len(run) >= stack_len:
                levels.append({
                    "ts": bar["ts"],
                    "side": "buy" if side == "buy_imb" else "sell",
                    "low": run[0]["price"],
                    "high": run[-1]["price"] + bar["row_size"],
                    "rows": len(run),
                })
    return levels


def session_stats(session: Any, underlying: str, day: datetime | None = None) -> dict:
    """Session totals for the header strip: buy/sell volume, delta, CVD path."""
    day = day or datetime.now(IST)
    start = day.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        session.query(OrderFlowBar)
        .filter(
            OrderFlowBar.underlying == underlying.upper(),
            OrderFlowBar.bucket_ts >= start,
        )
        .order_by(OrderFlowBar.bucket_ts)
        .all()
    )
    buy = sum(r.buy_qty or 0.0 for r in rows)
    sell = sum(r.sell_qty or 0.0 for r in rows)
    cvd: list[float] = []
    running = 0.0
    for r in rows:
        running += (r.buy_qty or 0.0) - (r.sell_qty or 0.0)
        cvd.append(running)
    return {
        "bars": len(rows),
        "buy": buy,
        "sell": sell,
        "delta": buy - sell,
        "volume": buy + sell,
        "cvd": cvd,
        "last_price": rows[-1].close if rows else None,
        "updated": rows[-1].bucket_ts if rows else None,
    }
