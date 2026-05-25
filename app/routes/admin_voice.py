"""Voice admin endpoints — kill switch, status, command history.

Routes:
  GET  /admin/voice/status   — channel config + runtime counters
  POST /admin/voice/toggle   — enable / disable voice channel
  GET  /admin/voice/history  — last N voice commands with decisions

Auth: X-Admin-Token header (ADMIN_AUTH_TOKEN env var).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.voice.audit import get_audit_logger
from app.voice.config import load_config, save_config
from app.voice.pending import get_store

_IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/voice", tags=["admin-voice"])


# Module-level settings getter — exposed so tests can override via dependency_overrides.
def _admin_settings():
    return get_settings()


# ── Auth dependency ────────────────────────────────────────────────────────────

def verify_admin_token(
    x_admin_token: str | None = Header(default=None),
    settings=Depends(_admin_settings),
) -> str:
    expected = settings.ADMIN_AUTH_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_AUTH_TOKEN not configured on server")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Admin-Token header")
    return x_admin_token


# ══════════════════════════════════════════════════════════════════════════════
# GET /admin/voice/status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def admin_status(
    token: str = Depends(verify_admin_token),
    settings=Depends(_admin_settings),
) -> JSONResponse:
    cfg      = load_config()
    store    = get_store()
    return JSONResponse({
        "voice_channel_enabled" : cfg.get("voice_channel_enabled", False),
        "after_hours_mode"      : cfg.get("after_hours_mode", "reject"),
        "max_voice_lots"        : settings.VOICE_MAX_LOTS,
        "pending_orders"        : store.count(),
        "confirm_ttl_seconds"   : settings.VOICE_CONFIRM_TTL_SECONDS,
        "voice_dedup_seconds"   : settings.VOICE_DEDUP_SECONDS,
        "voice_config_path"     : settings.VOICE_CONFIG_PATH,
        "transcription_mode"    : os.getenv("TRANSCRIPTION_MODE", "text_only"),
        "anthropic_key_set"     : bool(settings.ANTHROPIC_API_KEY),
        "openai_key_set"        : bool(settings.OPENAI_API_KEY),
        "nlu_model"             : settings.VOICE_NLU_MODEL,
        "allowed_instruments"   : list(settings.VOICE_ALLOWED_INSTRUMENTS),
    })


# ══════════════════════════════════════════════════════════════════════════════
# POST /admin/voice/toggle
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/toggle")
async def admin_toggle(
    request: Request,
    token: str = Depends(verify_admin_token),
) -> JSONResponse:
    audit = get_audit_logger()
    source_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")

    body = await request.json()
    cfg  = load_config()

    if "enabled" in body:
        new_state = bool(body["enabled"])
    else:
        new_state = not cfg.get("voice_channel_enabled", False)

    cfg["voice_channel_enabled"] = new_state
    save_config(cfg)

    state_str = "ENABLED" if new_state else "DISABLED"
    audit.info(f"TOGGLE voice_channel={state_str} from {source_ip}")

    return JSONResponse({
        "status"                : "ok",
        "voice_channel_enabled" : new_state,
        "message"               : f"Voice channel {state_str}",
    })


# ══════════════════════════════════════════════════════════════════════════════
# GET /admin/voice/history
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/history")
async def admin_history(
    limit: int = Query(default=20, ge=1, le=100),
    token: str = Depends(verify_admin_token),
) -> JSONResponse:
    store     = get_store()
    today_iso = datetime.now(_IST).date().isoformat()
    entries   = store.get_history(limit)
    orders_today = store.count_today(today_iso)
    return JSONResponse({
        "history"           : entries,
        "count"             : len(entries),
        "voice_orders_today": orders_today,
    })
