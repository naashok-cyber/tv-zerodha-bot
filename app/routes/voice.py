"""Voice trading endpoints — user-facing channel.

Routes:
  POST /voice/transcribe   — parse text/audio transcript → pending order
  POST /voice/confirm      — approve or reject a pending order
  POST /voice/cancel       — discard a pending order
  GET  /voice/pending      — list active pending orders

Auth: X-Voice-Auth-Token header (VOICE_AUTH_TOKEN env var).
Kill-switch: 403 if voice channel is disabled (POST /admin/voice/toggle).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.storage import get_db_session
from app.voice.audit import get_audit_logger
from app.voice.config import is_voice_enabled, load_config
from app.voice.nlu import build_system_prompt, call_nlu, transcribe_audio
from app.voice.pending import CONFIRM_TTL_SECONDS, VOICE_DEDUP_SECONDS, get_store

_IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


# Module-level settings getter — exposed so tests can override via dependency_overrides.
def _voice_settings():
    return get_settings()


# ── Auth dependency ────────────────────────────────────────────────────────────

def verify_voice_token(
    x_voice_auth_token: str | None = Header(default=None),
    settings=Depends(_voice_settings),
) -> str:
    expected = settings.VOICE_AUTH_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="VOICE_AUTH_TOKEN not configured on server")
    if not x_voice_auth_token or x_voice_auth_token != expected:
        get_audit_logger().warning("UNAUTHORIZED token missing or wrong")
        raise HTTPException(status_code=401, detail="Missing or invalid X-Voice-Auth-Token header")
    return x_voice_auth_token


def _gate_channel() -> None:
    if not is_voice_enabled():
        raise HTTPException(
            status_code=403,
            detail="Voice channel is disabled. Enable via: POST /admin/voice/toggle {\"enabled\": true}",
        )


# ── Market hours helper (IST) ──────────────────────────────────────────────────

def _is_market_open(underlying: str, mcx_instruments: list[str]) -> bool:
    now_ist = datetime.now(_IST)
    if now_ist.weekday() >= 5:
        return False
    t = now_ist.hour * 60 + now_ist.minute
    if underlying.upper() in [i.upper() for i in mcx_instruments]:
        return (9 * 60) <= t <= (23 * 60 + 30)
    return (9 * 60 + 15) <= t <= (15 * 60 + 30)


# ── Human-readable confirmation summary ───────────────────────────────────────

def _build_summary(entry: dict) -> str:
    action      = entry.get("action", "?")
    underlying  = entry.get("underlying", "?")
    qty         = entry.get("quantity", 1)
    opt_type    = entry.get("_option_type")
    strike      = entry.get("_strike")
    confidence  = float(entry.get("_confidence", 0))
    action_type = entry.get("action_type", "entry")
    uncertain   = entry.get("_uncertain_fields", [])

    lp         = entry.get("_limit_price")
    if action_type in ("exit_all", "square_off"):
        summary = f"EXIT ALL {underlying} positions — are you sure?"
    else:
        strike_str = f"strike {int(strike)}" if strike else "ATM (delta 0.65 — assumed)"
        opt_str    = f"{opt_type} " if opt_type else ""
        lp_str     = f" @ ₹{float(lp):.2f} limit" if lp else ""
        summary    = f"You want to {action} {qty} lot(s) of {underlying} {opt_str}[{strike_str}]{lp_str} — confirm?"

    notes = []
    if strike is None and action_type == "entry":
        notes.append("strike not specified → ATM via delta 0.65")
    if uncertain:
        notes.append(f"assumed: {', '.join(uncertain)}")
    if confidence < 0.85:
        notes.append(f"LOW CONFIDENCE {confidence:.0%} — review carefully")
    if notes:
        summary += "  ⚠ " + " | ".join(notes)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# POST /voice/transcribe
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/transcribe")
async def transcribe(
    request: Request,
    token: str = Depends(verify_voice_token),
    settings=Depends(_voice_settings),
    db: Session = Depends(get_db_session),
) -> JSONResponse:
    _gate_channel()

    store      = get_store()
    audit      = get_audit_logger()
    source_ip  = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")

    # Rate limit: 30 req / 60 s per token
    if not store.check_rate(token, limit=settings.VOICE_RATE_LIMIT):
        audit.warning(f"RATE_LIMIT ip:{source_ip}")
        raise HTTPException(status_code=429, detail="Rate limit exceeded (30 requests/min)")

    transcript: str | None = None

    # Path 1: pre-transcribed text (preferred)
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        transcript = (body or {}).get("text", "").strip() or None
    elif "multipart/form-data" in ct or "application/x-www-form-urlencoded" in ct:
        form = await request.form()
        transcript = (form.get("text") or "").strip() or None

    # Path 2: audio file → Whisper
    if not transcript:
        form = await request.form()
        audio_file = form.get("audio")
        if audio_file is None:
            raise HTTPException(status_code=400, detail="Provide 'text' field (pre-transcribed) or 'audio' file")
        transcription_mode = os.getenv("TRANSCRIPTION_MODE", "text_only")
        if transcription_mode == "text_only":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Audio transcription is disabled. "
                    "Set TRANSCRIPTION_MODE=whisper and OPENAI_API_KEY, "
                    "or send pre-transcribed text via the 'text' field."
                ),
            )
        openai_key = settings.OPENAI_API_KEY
        if not openai_key:
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")
        try:
            audio_bytes = await audio_file.read()
            if not audio_bytes:
                raise HTTPException(status_code=400, detail="Empty audio file")
            transcript = transcribe_audio(audio_bytes, audio_file.filename or "audio.wav", openai_key)
        except HTTPException:
            raise
        except Exception as exc:
            audit.error(f"TRANSCRIBE_FAILED ip:{source_ip} error:{exc}")
            raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")

    if not transcript:
        raise HTTPException(status_code=400, detail="Empty transcript — nothing to parse")

    audit.info(f"TRANSCRIPT ip:{source_ip} text:{transcript!r}")

    # NLU
    anthropic_key = settings.ANTHROPIC_API_KEY
    if not anthropic_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured on server")

    try:
        nlu = call_nlu(
            transcript,
            api_key=anthropic_key,
            model=settings.VOICE_NLU_MODEL,
            nfo_instruments=list(settings.VOICE_NFO_INSTRUMENTS),
            mcx_instruments=list(settings.VOICE_MCX_INSTRUMENTS),
        )
    except json.JSONDecodeError as exc:
        audit.error(f"NLU_BAD_JSON ip:{source_ip} error:{exc}")
        raise HTTPException(status_code=500, detail=f"NLU returned invalid JSON: {exc}")
    except Exception as exc:
        audit.error(f"NLU_FAILED ip:{source_ip} transcript:{transcript!r} error:{exc}")
        raise HTTPException(status_code=500, detail=f"NLU call failed: {exc}")

    confidence  = float(nlu.get("confidence", 0))
    action      = nlu.get("action", "")
    underlying  = nlu.get("underlying", "")
    quantity    = int(nlu.get("quantity", 1))
    action_type = nlu.get("action_type", "entry")
    exchange    = nlu.get("exchange", "NFO")

    allowed = set(settings.VOICE_ALLOWED_INSTRUMENTS)
    if underlying not in allowed:
        audit.warning(f"REJECTED_INSTRUMENT ip:{source_ip} instrument:{underlying!r}")
        raise HTTPException(
            status_code=400,
            detail=f"Instrument '{underlying}' not in whitelist: {sorted(allowed)}",
        )

    if action_type == "entry":
        if not _is_market_open(underlying, list(settings.VOICE_MCX_INSTRUMENTS)):
            after_hours = load_config().get("after_hours_mode", "reject")
            if after_hours == "reject":
                audit.warning(f"REJECTED_AFTER_HOURS ip:{source_ip} underlying:{underlying}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Market is closed for {underlying}. Voice orders rejected outside trading hours.",
                )
        if quantity > settings.VOICE_MAX_LOTS:
            audit.warning(f"REJECTED_QTY ip:{source_ip} qty:{quantity} max:{settings.VOICE_MAX_LOTS}")
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Quantity {quantity} exceeds VOICE_MAX_LOTS={settings.VOICE_MAX_LOTS}. "
                    "Place this order manually."
                ),
            )

    dedup_key = f"{action}|{underlying}|{quantity}"
    if store.is_duplicate(dedup_key):
        raise HTTPException(
            status_code=429,
            detail=f"Duplicate voice command (identical within {VOICE_DEDUP_SECONDS}s). Wait and retry.",
        )

    # ── Straddle pre-confirmation validation ──────────────────────────────────
    straddle_sv = None
    straddle_report = None
    straddle_spread_warn = False
    if action_type == "straddle_short":
        from app.voice.straddle import validate_straddle, build_validation_report
        import app.state as _st
        try:
            from app.kite_session import get_session_manager as _ksm
            _kite = _ksm().get_kite() if not _st.SESSION_INVALID else None
        except Exception:
            _kite = None

        if _kite is not None:
            try:
                straddle_sv = validate_straddle(
                    underlying, quantity, _kite, db, settings,
                    now=datetime.now(_IST),
                    exchange=exchange,
                )
                straddle_report = build_validation_report(straddle_sv)
                straddle_spread_warn = not straddle_sv.spread_ok

                # Hard-block on margin insufficiency — cannot force-override this
                if not straddle_sv.margin_ok:
                    audit.warning(
                        f"STRADDLE_MARGIN_BLOCK ip:{source_ip} "
                        f"underlying:{underlying} reason:{straddle_sv.block_reason}"
                    )
                    raise HTTPException(status_code=400, detail=straddle_sv.block_reason)

            except HTTPException:
                raise
            except RuntimeError as exc:
                audit.error(f"STRADDLE_VALIDATE_FAIL ip:{source_ip} error:{exc}")
                raise HTTPException(status_code=400, detail=f"Straddle validation failed: {exc}")
            except Exception as exc:
                audit.error(f"STRADDLE_VALIDATE_ERROR ip:{source_ip} error:{exc}")
                straddle_report = f"⚠ Validation unavailable ({exc}) — review manually"
        else:
            straddle_report = "⚠ Not logged in to broker — live spread/margin check skipped"

    conf_token     = str(uuid.uuid4())
    low_confidence = confidence < 0.85
    is_exit        = action_type in ("exit_all", "square_off")

    pending_entry = {
        "_expires"          : time.monotonic() + CONFIRM_TTL_SECONDS,
        "_token"            : conf_token,
        "_source_ip"        : source_ip,
        "_transcript"       : transcript,
        "_confidence"       : confidence,
        "_low_confidence"   : low_confidence,
        "_is_exit"          : is_exit,
        "_confirm_step"     : 1,
        "_option_type"      : nlu.get("option_type"),
        "_strike"           : nlu.get("strike"),
        "_limit_price"      : nlu.get("limit_price"),
        "_uncertain_fields" : nlu.get("uncertain_fields", []),
        # Straddle-specific (populated below when action_type == "straddle_short")
        "_straddle_ce_symbol"   : straddle_sv.ce.tradingsymbol if straddle_sv else None,
        "_straddle_pe_symbol"   : straddle_sv.pe.tradingsymbol if straddle_sv else None,
        "_straddle_atm_strike"  : straddle_sv.atm_strike if straddle_sv else None,
        "_straddle_expiry"      : str(straddle_sv.expiry) if straddle_sv else None,
        "_straddle_lot_size"    : straddle_sv.lot_size if straddle_sv else None,
        "_straddle_lot_units"   : straddle_sv.lot_units if straddle_sv else None,
        "_straddle_net_credit"  : straddle_sv.net_credit_per_lot if straddle_sv else None,
        "_straddle_est_sl"      : straddle_sv.estimated_sl_per_lot if straddle_sv else None,
        "_straddle_all_ok"      : straddle_sv.all_ok if straddle_sv else None,
        "_straddle_force_spread": False,
        "action_type"       : action_type,
        "action"            : action,
        "underlying"        : underlying,
        "quantity"          : quantity,
        "options_mode"      : True,
        "target_delta"      : float(nlu.get("target_delta", 0.65)),
        "exchange"          : nlu.get("exchange", "NFO"),
        "current_price"     : 0,
        "target"            : None,
        "stoploss"          : None,
        "symbol"            : "",
    }
    store.store(conf_token, pending_entry)
    summary = _build_summary(pending_entry)

    # Estimated SL / target for the confirmation card (display only — not enforced here)
    nlu_lp = nlu.get("limit_price")
    estimated_sl = estimated_target = None
    if nlu_lp is not None and action_type == "entry":
        lp_f = float(nlu_lp)
        rr   = settings.RR_RATIO
        is_ng = underlying.upper() in [n.upper() for n in settings.NATURAL_GAS_NAMES]
        if is_ng:
            sl_frac = settings.FUTURES_SL_PCT
            sl_dist = lp_f * sl_frac
            if action == "BUY":
                estimated_sl     = round(lp_f - sl_dist, 2)
                estimated_target = round(lp_f + rr * sl_dist, 2)
            else:
                estimated_sl     = round(lp_f + sl_dist, 2)
                estimated_target = round(lp_f - rr * sl_dist, 2)
        else:
            sl_frac          = settings.SL_PREMIUM_PCT
            sl_dist          = lp_f * sl_frac
            estimated_sl     = round(lp_f - sl_dist, 2)
            estimated_target = round(lp_f + rr * sl_dist, 2)

    audit.info(
        f"PENDING token:{conf_token} ip:{source_ip} action:{action} "
        f"underlying:{underlying} qty:{quantity} confidence:{confidence:.2f} "
        f"low_conf:{low_confidence} is_exit:{is_exit}"
    )

    store.add_history({
        "_token"          : conf_token,
        "ts"              : datetime.now(_IST).isoformat(),
        "transcript"      : transcript,
        "action"          : action,
        "action_type"     : action_type,
        "underlying"      : underlying,
        "quantity"        : quantity,
        "confidence"      : confidence,
        "low_confidence"  : low_confidence,
        "source_ip"       : source_ip,
        "token_short"     : conf_token[:8],
        "decision"        : "pending",
        "result"          : None,
    })

    return JSONResponse({
        "status"                  : "pending_confirmation",
        "confirmation_token"      : conf_token,
        "summary"                 : summary,
        "parsed_order"            : {
            k: v for k, v in nlu.items()
            if k not in ("confidence", "uncertain_fields", "action_type", "option_type")
        },
        "confidence"              : confidence,
        "low_confidence"          : low_confidence,
        "double_confirm_required" : is_exit,
        "expires_in_seconds"      : CONFIRM_TTL_SECONDS,
        "limit_price"             : nlu_lp,
        "estimated_sl"            : estimated_sl,
        "estimated_target"        : estimated_target,
        "straddle_report"         : straddle_report,
        "straddle_spread_warn"    : straddle_spread_warn,
    })


# ══════════════════════════════════════════════════════════════════════════════
# POST /voice/confirm
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/confirm")
async def confirm(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str = Depends(verify_voice_token),
    db: Session = Depends(get_db_session),
    settings=Depends(_voice_settings),
) -> JSONResponse:
    _gate_channel()

    store    = get_store()
    audit    = get_audit_logger()
    source_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")

    body         = await request.json()
    conf_tok     = body.get("confirmation_token", "")
    approved     = bool(body.get("approved", False))
    force_spread = bool(body.get("force_spread", False))

    if not conf_tok:
        raise HTTPException(status_code=400, detail="confirmation_token required")

    entry = store.get(conf_tok)
    if entry is None:
        audit.warning(f"EXPIRED_TOKEN token:{conf_tok} ip:{source_ip}")
        raise HTTPException(status_code=404, detail="Token not found or expired (60 s TTL)")

    if not approved:
        store.pop(conf_tok)
        audit.info(f"REJECTED token:{conf_tok} ip:{source_ip}")
        store.update_history(conf_tok, "rejected", None)
        return JSONResponse({"status": "rejected", "message": "Order cancelled by user"})

    # Re-check kill switch (channel may have been toggled off between parse and confirm)
    if not is_voice_enabled():
        store.pop(conf_tok)
        audit.warning(f"CHANNEL_DISABLED_AT_CONFIRM token:{conf_tok}")
        store.update_history(conf_tok, "channel_disabled", None)
        raise HTTPException(
            status_code=403,
            detail="Voice channel was disabled after this order was parsed — order cancelled.",
        )

    low_conf     = entry.get("_low_confidence", False)
    is_exit      = entry.get("_is_exit", False)
    confirm_step = entry.get("_confirm_step", 1)
    action_type  = entry.get("action_type", "entry")
    underlying   = entry.get("underlying", "")

    # Gate 1: low-confidence override required
    if low_conf and not body.get("low_confidence_override", False):
        return JSONResponse({
            "status"             : "low_confidence_confirmation_required",
            "message"            : (
                f"Confidence is {entry['_confidence']:.0%} — below the 85% threshold. "
                "Carefully review the parsed order, then resend with "
                "\"low_confidence_override\": true to proceed."
            ),
            "confirmation_token" : conf_tok,
            "summary"            : _build_summary(entry),
            "parsed_order"       : {
                k: v for k, v in entry.items()
                if not k.startswith("_") and k != "action_type"
            },
        })

    # Gate 2: double-confirm for EXIT_ALL / SQUARE_OFF
    if is_exit and confirm_step == 1:
        store.advance_confirm_step(conf_tok)
        audit.info(f"DOUBLE_CONFIRM_STEP1 token:{conf_tok} ip:{source_ip}")
        return JSONResponse({
            "status"             : "second_confirmation_required",
            "message"            : (
                f"⚠ SECOND CONFIRMATION REQUIRED — "
                f"You are about to exit ALL open {underlying} positions. "
                "This cannot be undone. Send approved: true again to confirm."
            ),
            "confirmation_token" : conf_tok,
        })

    # All gates passed — execute
    store.pop(conf_tok)

    from app.voice.executor import execute_voice_entry, execute_voice_exit, execute_voice_straddle

    try:
        if action_type in ("exit_all", "square_off"):
            result, status_code = execute_voice_exit(underlying, db, settings)
        elif action_type == "straddle_short":
            if force_spread:
                entry["_straddle_force_spread"] = True
            result, status_code = execute_voice_straddle(
                entry, conf_tok, background_tasks, db, settings
            )
        else:
            result, status_code = execute_voice_entry(
                entry, conf_tok, background_tasks, db, settings
            )
    except Exception as exc:
        audit.error(
            f"EXECUTE_FAILED token:{conf_tok} ip:{source_ip} "
            f"underlying:{underlying} error:{exc}"
        )
        store.update_history(conf_tok, "execute_exception", {"error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Order execution raised an exception: {exc}")

    decision = "approved_executed" if status_code < 400 else "approved_failed"
    audit.info(
        f"CONFIRMED token:{conf_tok} ip:{source_ip} action_type:{action_type} "
        f"underlying:{underlying} decision:{decision} result:{json.dumps(result)}"
    )
    store.update_history(conf_tok, decision, result)

    return JSONResponse(
        {
            "status"     : decision,
            "token"      : conf_tok,
            "result"     : result,
            "transcript" : entry.get("_transcript"),
            "confidence" : entry.get("_confidence"),
        },
        status_code=status_code,
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /voice/cancel
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/cancel")
async def cancel(
    request: Request,
    token: str = Depends(verify_voice_token),
) -> JSONResponse:
    _gate_channel()

    store     = get_store()
    audit     = get_audit_logger()
    source_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")

    body      = await request.json()
    conf_tok  = body.get("confirmation_token", "")
    if not conf_tok:
        raise HTTPException(status_code=400, detail="confirmation_token required")

    entry = store.pop(conf_tok)
    if entry is None:
        raise HTTPException(status_code=404, detail="Token not found or already expired")

    audit.info(f"CANCELLED token:{conf_tok} ip:{source_ip}")
    store.update_history(conf_tok, "cancelled", None)
    return JSONResponse({"status": "cancelled", "token": conf_tok})


# ══════════════════════════════════════════════════════════════════════════════
# GET /voice/pending
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/pending")
async def pending(
    token: str = Depends(verify_voice_token),
) -> JSONResponse:
    _gate_channel()
    store = get_store()
    store.expire_all()
    items = store.list_active()
    return JSONResponse({"pending_orders": items, "count": len(items)})
