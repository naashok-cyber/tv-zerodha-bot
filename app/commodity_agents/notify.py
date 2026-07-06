"""Telegram push for new actionable recommendations.

Reuses the existing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID settings (no-op when
empty). Telegram was chosen over web push for the iOS "on the go" requirement
because it's already in the stack, needs zero APNs/VAPID setup, and the
dashboard PWA covers the view/approve side.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 5


def send_telegram(settings: Any, text: str) -> bool:
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception as exc:
        log.warning("[notify] telegram send failed: %s", exc)
        return False


def notify_recommendation(settings: Any, rec_dict: dict) -> bool:
    """Push only actionable, sufficiently confident recommendations —
    NO_TRADE and low-confidence outputs stay in the dashboard, not the phone."""
    if rec_dict.get("direction") == "NO_TRADE":
        return False
    conf = rec_dict.get("confidence") or 0.0
    if conf < settings.COMMODITY_NOTIFY_MIN_CONFIDENCE:
        return False
    strikes = ", ".join(rec_dict.get("strikes") or []) or "—"
    text = (
        f"<b>{rec_dict['commodity']}</b> — {rec_dict['direction']} "
        f"{rec_dict['strategy_type']} (conf {conf:.0%})\n"
        f"Strikes: {strikes}\n"
        f"{(rec_dict.get('reasoning_summary') or '')[:400]}\n\n"
        f"Open the dashboard to approve/reject. Recommendation "
        f"#{rec_dict.get('id', '?')}"
    )
    return send_telegram(settings, text)
