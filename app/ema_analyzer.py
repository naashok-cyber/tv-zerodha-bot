"""EMA trend/crossover analyzer — 1-min monitor that buys long options on a
confirmed EMA crossover and rides a fixed ₹ SL / ₹ target GTT band.

Signal (pure, in `compute_signal`, unit-tested):
  1. EMA-fast (default 9) and EMA-slow (default 39) over 1-min closes.
  2. Crossover — the fast EMA crosses the slow EMA between the last two bars.
  3. Trend slope — the slow EMA's % change over the last `slope_lookback` bars
     must exceed `slope_min_pct` in the crossover's direction (the trend filter
     that keeps chop from firing every whipsaw cross).
  A BUY fires only when a bullish crossover coincides with a rising slope; a
  SELL only when a bearish crossover coincides with a falling slope.

Execution (`run_ema_analyzer_job`, 1-min cron):
  BUY  → long CE, SELL → long PE, always bought (never written) regardless of
  the global trade mode, by routing an AlertPayload with an explicit option_type
  and BUY action through the normal `_process_alert` pipeline (risk guards,
  strike selection, sizing, GTT, paper/live — all reused). The fixed ₹ bands
  ride along on the payload (sl_inr / target_inr) and become the GTT OCO
  triggers in `_on_entry_filled`.

  One position at a time per underlying (per mode). Exits are the GTT OCO bands
  only, unless EMA_ANALYZER_EXIT_ON_OPPOSITE is set, in which case an opposite
  crossover closes the open leg (and the fresh signal re-enters the other way).

Default OFF and paper-first: enable at /control once the paper scorecard
(strategy_id `ema_analyzer_<UNDERLYING>`) shows an edge.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

_LOG_PREFIX = "[ema_analyzer]"


# ── Pure signal engine ────────────────────────────────────────────────────────

def ema_series(values: list[float], period: int) -> list[float | None]:
    """EMA aligned to `values`; None for the warm-up bars before it is defined.

    Seeded with the SMA of the first `period` values (TradingView's ta.ema
    initialisation), then multiplier k = 2/(period+1).
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period or period <= 0:
        return out
    k = 2.0 / (period + 1)
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    ema = sma
    for i in range(period, n):
        ema = values[i] * k + ema * (1.0 - k)
        out[i] = ema
    return out


@dataclass(frozen=True)
class AnalyzerSignal:
    action: str | None          # "BUY" (long CE), "SELL" (long PE), or None
    reason: str
    ema_fast: float | None = None
    ema_slow: float | None = None
    slope_pct: float | None = None
    cross_up: bool = False
    cross_dn: bool = False
    last_close: float | None = None


def compute_signal(
    candles: list[dict],
    *,
    fast: int = 9,
    slow: int = 39,
    slope_lookback: int = 5,
    slope_min_pct: float = 0.0003,
) -> AnalyzerSignal:
    """Combine EMA crossover with the slow-EMA slope into a BUY/SELL/None signal.

    `candles` are completed OHLC dicts (need "close"), oldest first. Returns an
    AnalyzerSignal with action=None when there is no confirmed entry.
    """
    if fast >= slow:
        return AnalyzerSignal(None, f"fast({fast}) must be < slow({slow})")

    closes = [float(c["close"]) for c in candles]
    # Need two adjacent defined slow-EMA points (for the cross) plus enough
    # history behind them for the slope lookback.
    if len(closes) < slow + slope_lookback + 1:
        return AnalyzerSignal(None, f"insufficient candles ({len(closes)})")

    ema_f = ema_series(closes, fast)
    ema_s = ema_series(closes, slow)

    f_now, f_prev = ema_f[-1], ema_f[-2]
    s_now, s_prev = ema_s[-1], ema_s[-2]
    s_ref = ema_s[-1 - slope_lookback]
    if None in (f_now, f_prev, s_now, s_prev, s_ref):
        return AnalyzerSignal(None, "EMA warm-up incomplete")

    cross_up = f_prev <= s_prev and f_now > s_now
    cross_dn = f_prev >= s_prev and f_now < s_now
    slope_pct = (s_now - s_ref) / s_ref if s_ref else 0.0
    last_close = closes[-1]

    base = dict(
        ema_fast=f_now, ema_slow=s_now, slope_pct=slope_pct,
        cross_up=cross_up, cross_dn=cross_dn, last_close=last_close,
    )

    if cross_up and slope_pct >= slope_min_pct:
        return AnalyzerSignal(
            "BUY",
            f"bullish cross + slope {slope_pct*100:.3f}% ≥ {slope_min_pct*100:.3f}%",
            **base,
        )
    if cross_dn and slope_pct <= -slope_min_pct:
        return AnalyzerSignal(
            "SELL",
            f"bearish cross + slope {slope_pct*100:.3f}% ≤ -{slope_min_pct*100:.3f}%",
            **base,
        )

    if cross_up or cross_dn:
        reason = (
            f"cross {'up' if cross_up else 'down'} but slope "
            f"{slope_pct*100:.3f}% fails ±{slope_min_pct*100:.3f}% trend filter"
        )
    else:
        reason = "no crossover"
    return AnalyzerSignal(None, reason, **base)


# ── Telegram (best-effort; no-op without creds) ───────────────────────────────

def _notify(settings: Any, text: str) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as exc:  # pragma: no cover - network best-effort
        log.warning("%s telegram notify failed: %s", _LOG_PREFIX, exc)


# ── Candle sourcing ───────────────────────────────────────────────────────────

def _resolve_chart_instrument(underlying_name: str, session: Any, now: datetime) -> Any:
    """Instrument whose 1-min candles drive the signal.

    NSE indices use the spot index instrument (e.g. "NIFTY 50"); MCX commodities
    use the near-month futures contract. Returns None when unresolved.
    """
    from app.storage import Instrument
    from app.symbol_mapper import resolve_underlying

    underlying = resolve_underlying(underlying_name)
    if underlying.segment == "MCX":
        return (
            session.query(Instrument)
            .filter(
                Instrument.name == underlying.name,
                Instrument.instrument_type == "FUT",
                Instrument.exchange == "MCX",
                Instrument.expiry >= now.date(),
            )
            .order_by(Instrument.expiry)
            .first()
        )
    parts = underlying.spot_source.split(":", 1)
    exch, ts = (parts[0], parts[1]) if len(parts) == 2 else ("NSE", parts[0])
    return (
        session.query(Instrument)
        .filter(Instrument.exchange == exch, Instrument.tradingsymbol == ts)
        .first()
    )


def _fetch_completed_candles(
    kite: Any, token: int, now: datetime, interval: str, min_needed: int,
) -> list[dict]:
    """Fetch 1-min candles and drop the still-forming current-minute bar.

    Pulls a wide window (continuous across days, matching a TradingView 1-min
    chart) and returns only bars that have closed, so a signal never acts on a
    partial bar.
    """
    from_dt = now - timedelta(days=4)
    try:
        raw = kite.historical_data(token, from_dt, now, interval)
    except Exception as exc:
        log.warning("%s historical_data failed for token %s: %s", _LOG_PREFIX, token, exc)
        return []
    if not raw:
        return []
    cutoff = now.replace(second=0, microsecond=0)
    completed = [c for c in raw if c.get("date") is not None and c["date"] < cutoff]
    if len(completed) < min_needed:
        log.info(
            "%s only %d completed candles (need %d) — skipping",
            _LOG_PREFIX, len(completed), min_needed,
        )
    return completed


# ── Position gating ───────────────────────────────────────────────────────────

def _open_analyzer_position(session: Any, strategy_id: str, paper: bool) -> Any:
    """Return this strategy's open Position (current mode), or None."""
    from app.storage import Alert, ClosedTrade, Order, Position

    return (
        session.query(Position)
        .join(Order, Position.order_id == Order.id)
        .join(Alert, Alert.id == Order.alert_id)
        .outerjoin(ClosedTrade, Position.id == ClosedTrade.position_id)
        .filter(
            Alert.strategy_id == strategy_id,
            ClosedTrade.id == None,  # noqa: E711
            Order.dry_run == paper,
        )
        .order_by(Position.opened_at.desc())
        .first()
    )


# ── Alert construction / routing ──────────────────────────────────────────────

def _build_payload(
    symbol: str, action: str, price: float, now: datetime, settings: Any,
    *, option_type: str | None = None, with_bands: bool = False,
) -> Any:
    from app.schemas import AlertPayload

    fields: dict[str, Any] = {
        "symbol": symbol,
        "action": action,
        "instrument_type": "OPTIONS",
        "price": Decimal(str(round(price, 2))),
        "option_type": option_type,
        "timeframe": "1",
        "alert_id": f"ema-{uuid.uuid4().hex[:12]}",
        "timestamp": now,
    }
    if with_bands:
        fields["sl_inr"] = Decimal(str(settings.EMA_ANALYZER_SL_INR))
        fields["target_inr"] = Decimal(str(settings.EMA_ANALYZER_TARGET_INR))
    return AlertPayload(**fields)


def _persist_and_process(
    session_factory: Any, settings: Any, strategy_id: str, payload: Any, now: datetime,
) -> None:
    """Insert the Alert row, then run it through the shared alert pipeline."""
    from app.storage import Alert

    with session_factory() as session:
        alert = Alert(
            received_at=now,
            strategy_id=strategy_id,
            tv_ticker=payload.symbol,
            tv_exchange="",
            action=payload.action,
            order_type=None,
            entry_price=float(payload.price),
            stop_loss=None,
            sl_percent=None,
            atr=None,
            quantity_hint=None,
            product="NRML",
            tv_time=now.isoformat(),
            bar_time=None,
            interval="1",
            idempotency_key=str(uuid.uuid4()),
            raw_payload="{}",
            processed=False,
        )
        session.add(alert)
        session.commit()
        session.refresh(alert)
        alert_id = alert.id

    from app.main import _process_alert
    _process_alert(alert_id, payload, settings)


# ── 1-min job ─────────────────────────────────────────────────────────────────

def run_ema_analyzer_job(settings: Any, session_factory: Any) -> None:
    """Evaluate the EMA signal for each configured underlying and act on it."""
    import app.state as state

    if not state.is_ema_analyzer_enabled(settings.EMA_ANALYZER_ENABLED):
        return
    if state.is_emergency_stop():
        return
    if state.get_session_invalid():
        log.debug("%s session invalid — skipping", _LOG_PREFIX)
        return

    from app.kite_session import get_session_manager
    from app.config import IST

    try:
        kite = get_session_manager().get_kite()
    except Exception as exc:
        log.debug("%s no kite client: %s", _LOG_PREFIX, exc)
        return

    paper = state.is_paper_mode(settings.DRY_RUN)
    now = datetime.now(IST)
    interval = settings.EMA_ANALYZER_CANDLE_INTERVAL
    fast, slow = settings.EMA_ANALYZER_FAST, settings.EMA_ANALYZER_SLOW
    lookback = settings.EMA_ANALYZER_SLOPE_LOOKBACK
    slope_min = settings.EMA_ANALYZER_SLOPE_MIN_PCT
    min_needed = slow + lookback + 1

    for underlying_name in settings.EMA_ANALYZER_UNDERLYINGS:
        try:
            _process_underlying(
                underlying_name, kite, session_factory, settings,
                now, interval, fast, slow, lookback, slope_min, min_needed, paper,
            )
        except Exception as exc:
            log.error("%s %s: tick failed: %s", _LOG_PREFIX, underlying_name, exc, exc_info=True)


def _process_underlying(
    underlying_name: str, kite: Any, session_factory: Any, settings: Any,
    now: datetime, interval: str, fast: int, slow: int, lookback: int,
    slope_min: float, min_needed: int, paper: bool,
) -> None:
    strategy_id = f"ema_analyzer_{underlying_name}"

    with session_factory() as session:
        instr = _resolve_chart_instrument(underlying_name, session, now)
        if instr is None:
            log.warning("%s %s: no chart instrument found", _LOG_PREFIX, underlying_name)
            return
        token = instr.instrument_token
        open_pos = _open_analyzer_position(session, strategy_id, paper)
        open_type = open_pos.instrument_type if open_pos else None
        open_symbol = open_pos.tradingsymbol if open_pos else None

    candles = _fetch_completed_candles(kite, token, now, interval, min_needed)
    if len(candles) < min_needed:
        return

    sig = compute_signal(
        candles, fast=fast, slow=slow,
        slope_lookback=lookback, slope_min_pct=slope_min,
    )
    log.info(
        "%s %s: close=%.2f ema%d=%s ema%d=%s slope=%s → %s (%s)",
        _LOG_PREFIX, underlying_name, sig.last_close or 0.0,
        fast, f"{sig.ema_fast:.2f}" if sig.ema_fast is not None else "—",
        slow, f"{sig.ema_slow:.2f}" if sig.ema_slow is not None else "—",
        f"{sig.slope_pct*100:.3f}%" if sig.slope_pct is not None else "—",
        sig.action or "flat", sig.reason,
    )
    if sig.action is None:
        return

    # One position at a time. If already holding, only act when the opt-in
    # opposite-exit is on AND the new signal is the other direction.
    if open_pos is not None:
        held_dir = "BUY" if open_type == "CE" else "SELL"
        if not settings.EMA_ANALYZER_EXIT_ON_OPPOSITE or sig.action == held_dir:
            log.info(
                "%s %s: holding %s — skipping %s (one position at a time)",
                _LOG_PREFIX, underlying_name, open_type, sig.action,
            )
            return
        # Opposite crossover → close the held leg, then fall through to reverse.
        log.info(
            "%s %s: opposite signal %s vs held %s — closing %s",
            _LOG_PREFIX, underlying_name, sig.action, open_type, open_symbol,
        )
        exit_payload = _build_payload(open_symbol, "EXIT", sig.last_close, now, settings)
        _persist_and_process(session_factory, settings, strategy_id, exit_payload, now)
        _notify(
            settings,
            f"{_LOG_PREFIX} {underlying_name}: closed {open_symbol} on opposite crossover",
        )

    if settings.EMA_ANALYZER_SKIP_EXPIRY_DAY and _is_expiry_day(underlying_name, now):
        log.info(
            "%s %s: expiry day — skipping new long-option entry",
            _LOG_PREFIX, underlying_name,
        )
        return

    option_type = "CE" if sig.action == "BUY" else "PE"
    entry_payload = _build_payload(
        underlying_name, "BUY", sig.last_close, now, settings,
        option_type=option_type, with_bands=True,
    )
    log.info(
        "%s %s: %s → buy %s (SL ₹%.0f / target ₹%.0f) — %s",
        _LOG_PREFIX, underlying_name, sig.action, option_type,
        settings.EMA_ANALYZER_SL_INR, settings.EMA_ANALYZER_TARGET_INR, sig.reason,
    )
    _persist_and_process(session_factory, settings, strategy_id, entry_payload, now)
    _notify(
        settings,
        f"{_LOG_PREFIX} {underlying_name}: {sig.action} → buy {option_type} "
        f"@ close {sig.last_close:.2f} ({sig.reason})",
    )


def _is_expiry_day(underlying_name: str, now: datetime) -> bool:
    """True if an NSE/NFO option on `underlying_name` expires today."""
    from app.symbol_mapper import resolve_underlying

    underlying = resolve_underlying(underlying_name)
    if underlying.segment not in ("NSE", "NFO"):
        return False
    from app.storage import Instrument
    from app.main import _SessionFactory

    if _SessionFactory is None:
        return False
    with _SessionFactory() as session:
        return (
            session.query(Instrument)
            .filter(
                Instrument.name == underlying.name,
                Instrument.exchange.in_(["NSE", "NFO"]),
                Instrument.expiry == now.date(),
                Instrument.instrument_type.in_(["CE", "PE"]),
            )
            .first()
        ) is not None
