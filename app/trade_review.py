"""Post-hoc trade review notifier.

Fires once per REAL ClosedTrade insert via a SQLAlchemy after_commit listener.
Pulls the trade + its day-context (regime, other closes, IV drift), asks the
configured Claude model for a short human review, and pushes the result to
Telegram. Read-only: this module never imports orders/state setters, so a bug
here cannot influence trading decisions.

The listener is registered from main.py's lifespan via `register_hook()`. It
filters to `dry_run == False` and drops the work into a daemon thread so the
fill path is never blocked by the LLM call.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

_PREFIX = "[trade_review]"

# Prevent duplicate reviews if the same ClosedTrade is somehow flushed twice
# in one process lifetime (belt-and-braces against listener re-registration).
_reviewed_ids: set[int] = set()
_reviewed_lock = threading.Lock()


def _regime_hint(underlying: str | None) -> str:
    """Best-effort ADX regime read from delta_hedge's in-process cache.

    Returns "CHOPPY"/"MILD"/"TRENDING" if fresh, else "UNKNOWN". We never call
    Kite from the review path — that would put this job in the historical-data
    rate-limit bucket alongside the delta-hedge cron.
    """
    if not underlying:
        return "UNKNOWN"
    try:
        from app.delta_hedge import _ADX_CACHE, _ADX_CACHE_TTL_SEC
        import time as _time
        cached = _ADX_CACHE.get(underlying)
        if not cached:
            return "UNKNOWN"
        ts, _mult, _adx, regime = cached
        if (_time.monotonic() - ts) > _ADX_CACHE_TTL_SEC:
            return "UNKNOWN"
        return regime or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _iv_drift(session: Any, underlying: str | None, opened_at, closed_at) -> str:
    """Compare mean straddle IV in ±10min windows around entry vs exit.
    Returns "IV rose N%" / "IV fell N%" / "IV flat" / "" if no data."""
    if not underlying:
        return ""
    try:
        from app.storage import IvSnapshot
        entry_win = (
            session.query(IvSnapshot.iv_pct)
            .filter(
                IvSnapshot.underlying == underlying,
                IvSnapshot.at.between(opened_at - timedelta(minutes=10), opened_at + timedelta(minutes=10)),
                IvSnapshot.iv_pct.is_not(None),
            )
            .all()
        )
        exit_win = (
            session.query(IvSnapshot.iv_pct)
            .filter(
                IvSnapshot.underlying == underlying,
                IvSnapshot.at.between(closed_at - timedelta(minutes=10), closed_at + timedelta(minutes=10)),
                IvSnapshot.iv_pct.is_not(None),
            )
            .all()
        )
        if not entry_win or not exit_win:
            return ""
        ev = sum(r[0] for r in entry_win) / len(entry_win)
        xv = sum(r[0] for r in exit_win) / len(exit_win)
        if ev <= 0:
            return ""
        change = (xv - ev) / ev * 100
        if abs(change) < 2:
            return "IV flat"
        return f"IV {'rose' if change > 0 else 'fell'} {abs(change):.0f}%"
    except Exception as exc:
        log.debug("%s iv drift lookup failed: %s", _PREFIX, exc)
        return ""


def _todays_closes(session: Any, closed_at, exclude_id: int) -> list[dict]:
    """Every real ClosedTrade earlier the same day — lets the reviewer see the
    day's arc rather than judging one trade in isolation."""
    try:
        from app.storage import ClosedTrade
        day_start = closed_at.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = (
            session.query(ClosedTrade)
            .filter(
                ClosedTrade.closed_at >= day_start,
                ClosedTrade.closed_at <= closed_at,
                ClosedTrade.dry_run == False,  # noqa: E712
                ClosedTrade.id != exclude_id,
            )
            .order_by(ClosedTrade.closed_at.asc())
            .all()
        )
        return [
            {"sym": r.tradingsymbol, "pnl": float(r.pnl), "reason": r.exit_reason}
            for r in rows
        ]
    except Exception as exc:
        log.debug("%s todays_closes lookup failed: %s", _PREFIX, exc)
        return []


def _build_prompt(ct_row: dict, position_row: dict, entry_alert: dict | None,
                  regime: str, iv_note: str, todays: list[dict]) -> tuple[str, str]:
    """Return (system, user) prompts for the review model."""
    system = (
        "You are a trading review assistant for an MCX options + NG/CRUDE "
        "trader. You get one closed trade plus today's context. Reply in "
        "PLAIN TEXT (no markdown fences) with EXACTLY these 4 lines:\n"
        "VERDICT: Good | Neutral | Poor\n"
        "WHY: <one sentence, under 100 chars>\n"
        "NEXT TIME: <one specific, actionable tweak, under 100 chars>\n"
        "REGIME NOTE: <if regime is CHOPPY, say 'avoid fresh entries — market "
        "choppy'; if TRENDING, name the likely direction and say 'bias with "
        "trend on next entry'; if MILD or UNKNOWN, say 'no strong regime "
        "signal'>\n"
        "You never suggest specific orders — this is a notification, not a "
        "trading signal. The trader executes independently."
    )

    duration_min = (ct_row["closed_at"] - ct_row["opened_at"]).total_seconds() / 60
    other_summary = ""
    if todays:
        gainers = sum(1 for t in todays if t["pnl"] > 0)
        losers = sum(1 for t in todays if t["pnl"] <= 0)
        day_pnl = sum(t["pnl"] for t in todays) + ct_row["pnl"]
        other_summary = (
            f"\nDay so far: {len(todays)} prior closes "
            f"({gainers} winners, {losers} losers), running PnL "
            f"₹{day_pnl:+,.0f} incl. this trade."
        )

    entry_note = ""
    if entry_alert:
        entry_note = (
            f"\nEntry signal: action={entry_alert.get('action')} "
            f"option_type={entry_alert.get('option_type')} "
            f"strategy_id={entry_alert.get('strategy_id')}"
        )

    user = (
        f"CLOSED TRADE\n"
        f"  symbol: {ct_row['tradingsymbol']} ({ct_row['exchange']})\n"
        f"  underlying: {position_row.get('underlying') or '—'}\n"
        f"  quantity: {position_row.get('quantity')}\n"
        f"  entry premium: ₹{ct_row['entry_premium']:.2f}\n"
        f"  exit premium:  ₹{ct_row['exit_premium']:.2f}\n"
        f"  PnL: ₹{ct_row['pnl']:+,.2f}\n"
        f"  exit reason: {ct_row['exit_reason']}\n"
        f"  opened: {ct_row['opened_at'].strftime('%H:%M IST')}\n"
        f"  closed: {ct_row['closed_at'].strftime('%H:%M IST')} "
        f"(held {duration_min:.0f} min)\n"
        f"  strategy_id: {ct_row.get('strategy_id') or '—'}"
        f"{entry_note}\n\n"
        f"CONTEXT\n"
        f"  ADX regime now: {regime}\n"
        f"  IV move entry→exit: {iv_note or 'n/a'}"
        f"{other_summary}"
    )
    return system, user


def _call_llm(settings: Any, system: str, user: str) -> str | None:
    """One Anthropic call. Returns plain text or None on failure."""
    try:
        import anthropic  # runtime import — optional dep
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=settings.TRADE_REVIEW_MODEL,
            max_tokens=settings.TRADE_REVIEW_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return parts[-1].strip() if parts else None
    except Exception as exc:
        log.warning("%s llm call failed: %s", _PREFIX, exc)
        return None


def _format_telegram(ct_row: dict, review: str) -> str:
    """HTML-formatted Telegram body. `review` is the raw 4-line LLM reply."""
    pnl_sign = "🟢" if ct_row["pnl"] >= 0 else "🔴"
    return (
        f"{pnl_sign} <b>Trade Review</b> — {ct_row['tradingsymbol']}\n"
        f"PnL: <b>₹{ct_row['pnl']:+,.0f}</b> "
        f"({ct_row['exit_reason']})\n\n"
        f"<pre>{review}</pre>"
    )


def _review_worker(session_factory: Any, settings: Any, closed_trade_id: int) -> None:
    """Runs in a daemon thread. Any exception is swallowed — this must never
    affect trading."""
    try:
        from app.storage import Alert, ClosedTrade, Order, Position

        with session_factory() as session:
            ct = session.get(ClosedTrade, closed_trade_id)
            if ct is None:
                log.warning("%s ClosedTrade %d vanished before review", _PREFIX, closed_trade_id)
                return
            if ct.dry_run:
                return  # belt-and-braces; listener already filters

            position = session.get(Position, ct.position_id)
            entry_alert_dict: dict | None = None
            if position is not None:
                order = session.get(Order, position.order_id)
                if order is not None:
                    alert = (
                        session.query(Alert)
                        .filter(Alert.strategy_id == ct.strategy_id)
                        .order_by(Alert.received_at.desc())
                        .first()
                    ) if ct.strategy_id else None
                    if alert is not None:
                        entry_alert_dict = {
                            "action": alert.action,
                            "option_type": alert.option_type,
                            "strategy_id": alert.strategy_id,
                        }

            ct_row = {
                "id": ct.id,
                "tradingsymbol": ct.tradingsymbol,
                "exchange": ct.exchange,
                "entry_premium": float(ct.entry_premium),
                "exit_premium": float(ct.exit_premium),
                "pnl": float(ct.pnl),
                "exit_reason": ct.exit_reason,
                "opened_at": ct.opened_at,
                "closed_at": ct.closed_at,
                "strategy_id": ct.strategy_id,
            }
            position_row = {
                "underlying": position.underlying if position else None,
                "quantity": position.quantity if position else None,
            }

            underlying = position.underlying if position else None
            regime = _regime_hint(underlying)
            iv_note = _iv_drift(session, underlying, ct.opened_at, ct.closed_at)
            todays = _todays_closes(session, ct.closed_at, exclude_id=ct.id)

        system, user = _build_prompt(ct_row, position_row, entry_alert_dict,
                                     regime, iv_note, todays)
        review = _call_llm(settings, system, user)
        if not review:
            log.info("%s no review generated for ClosedTrade %d — skipping notify",
                     _PREFIX, closed_trade_id)
            return

        from app.commodity_agents.notify import send_telegram
        text = _format_telegram(ct_row, review)
        ok = send_telegram(settings, text)
        log.info("%s reviewed ClosedTrade %d (%s pnl=%+.0f) telegram=%s",
                 _PREFIX, closed_trade_id, ct_row["tradingsymbol"],
                 ct_row["pnl"], "ok" if ok else "failed")
    except Exception as exc:
        log.error("%s worker crashed for ClosedTrade %d: %s",
                  _PREFIX, closed_trade_id, exc, exc_info=True)


_INFO_KEY = "_pending_trade_reviews"


def register_hook(session_factory: Any, settings: Any) -> None:
    """Attach an after_commit listener to Session. Idempotent per process:
    a second call is a no-op so lifespan restarts don't stack listeners."""
    if not settings.TRADE_REVIEW_ENABLED:
        log.info("%s TRADE_REVIEW_ENABLED=false — hook not installed", _PREFIX)
        return

    from sqlalchemy import event
    from sqlalchemy.orm import Session as _SASession

    from app.storage import ClosedTrade

    # Idempotency guard — SQLAlchemy would happily register the same callable
    # twice, which would fire two reviews per trade.
    if getattr(register_hook, "_installed", False):
        log.info("%s hook already installed — skipping re-registration", _PREFIX)
        return

    @event.listens_for(_SASession, "after_flush")
    def _capture(session, flush_context):  # noqa: ARG001
        ids = session.info.setdefault(_INFO_KEY, [])
        for obj in session.new:
            if isinstance(obj, ClosedTrade) and not obj.dry_run and obj.id is not None:
                ids.append(obj.id)

    @event.listens_for(_SASession, "after_commit")
    def _fire(session):
        ids = session.info.pop(_INFO_KEY, None)
        if not ids:
            return
        for ct_id in ids:
            with _reviewed_lock:
                if ct_id in _reviewed_ids:
                    continue
                _reviewed_ids.add(ct_id)
            threading.Thread(
                target=_review_worker,
                args=(session_factory, settings, ct_id),
                daemon=True,
                name=f"trade-review-{ct_id}",
            ).start()

    @event.listens_for(_SASession, "after_rollback")
    def _clear(session):
        session.info.pop(_INFO_KEY, None)

    register_hook._installed = True  # type: ignore[attr-defined]
    log.info("%s hook installed (model=%s)", _PREFIX, settings.TRADE_REVIEW_MODEL)
