"""voice_handler.py — Voice command trading channel.

New endpoints (all additive, do not touch /webhook):
  POST /voice/transcribe      — parse transcript or audio → pending order
  POST /voice/confirm         — approve/reject a pending order
  POST /voice/cancel          — cancel a pending order
  GET  /voice/pending         — list pending orders
  GET  /admin/voice/status    — channel status + config
  POST /admin/voice/toggle    — enable / disable voice channel (kill switch)
  GET  /admin/voice/history   — last 20 voice commands with decisions

Auth:
  /voice/*  endpoints → X-Voice-Auth-Token header == $VOICE_AUTH_TOKEN
  /admin/*  endpoints → X-Admin-Token header     == $ADMIN_AUTH_TOKEN

# SECURITY NOTE: The existing /webhook endpoint has no auth.
# TradingView supports custom headers (Settings → Alerts → Webhook → Headers).
# Add an X-TV-Secret header check to /webhook as a follow-up task.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from threading import Lock

from flask import Blueprint, jsonify, request

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIRM_TTL_SECONDS    = 60
VOICE_DEDUP_SECONDS    = 10
MAX_VOICE_LOTS         = int(os.getenv("MAX_VOICE_LOTS", "5"))
CONFIG_PATH            = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
VOICE_AUTH_TOKEN_ENV   = "VOICE_AUTH_TOKEN"
ADMIN_AUTH_TOKEN_ENV   = "ADMIN_AUTH_TOKEN"
ANTHROPIC_API_KEY_ENV  = "ANTHROPIC_API_KEY"
OPENAI_API_KEY_ENV     = "OPENAI_API_KEY"
TRANSCRIPTION_MODE_ENV = "TRANSCRIPTION_MODE"   # "text_only" (default) | "whisper"
CLAUDE_MODEL           = "claude-sonnet-4-5"

# IST = UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))

# ── Audit logger ───────────────────────────────────────────────────────────────
def _build_audit_logger() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger("voice_audit")
    if not logger.handlers:
        handler = RotatingFileHandler(
            "logs/voice_audit.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger

_audit = _build_audit_logger()

# ── Config persistence ─────────────────────────────────────────────────────────
_config_lock = Lock()

def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"voice_channel_enabled": False, "after_hours_mode": "reject"}

def _save_config(cfg: dict) -> None:
    with _config_lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)

def is_voice_enabled() -> bool:
    return bool(_load_config().get("voice_channel_enabled", False))

# ── Rate limiter (30 req / 60 s per token) ────────────────────────────────────
_rate_store: dict[str, deque] = {}
_rate_lock = Lock()

def _rate_check(token: str, limit: int = 30, window_sec: int = 60) -> bool:
    now = time.monotonic()
    with _rate_lock:
        q = _rate_store.setdefault(token, deque())
        while q and now - q[0] > window_sec:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
    return True

# ── Auth helpers ───────────────────────────────────────────────────────────────
def _check_voice_auth() -> bool:
    expected = os.getenv(VOICE_AUTH_TOKEN_ENV, "")
    if not expected:
        return False
    return request.headers.get("X-Voice-Auth-Token", "") == expected

def _check_admin_auth() -> bool:
    expected = os.getenv(ADMIN_AUTH_TOKEN_ENV, "")
    if not expected:
        return False
    return request.headers.get("X-Admin-Token", "") == expected

# ── Pending orders store ───────────────────────────────────────────────────────
_pending: dict[str, dict] = {}
_pending_lock = Lock()

def _store_pending(token: str, entry: dict) -> None:
    with _pending_lock:
        _pending[token] = entry

def _get_pending(token: str) -> dict | None:
    with _pending_lock:
        entry = _pending.get(token)
    if entry is None:
        return None
    if time.monotonic() > entry["_expires"]:
        with _pending_lock:
            _pending.pop(token, None)
        return None
    return entry

def _pop_pending(token: str) -> dict | None:
    with _pending_lock:
        return _pending.pop(token, None)

def _expire_pending() -> None:
    now = time.monotonic()
    with _pending_lock:
        stale = [k for k, v in _pending.items() if now > v["_expires"]]
        for k in stale:
            _pending.pop(k, None)

# ── Voice dedup (10 s per action|underlying|quantity key) ─────────────────────
_voice_dedup: dict[str, float] = {}
_dedup_lock = Lock()

def _voice_is_duplicate(key: str) -> bool:
    now = time.monotonic()
    with _dedup_lock:
        last = _voice_dedup.get(key, 0)
        if now - last < VOICE_DEDUP_SECONDS:
            return True
        _voice_dedup[key] = now
    return False

# ── Command history (last 100 entries, newest first) ──────────────────────────
_history: deque = deque(maxlen=100)
_history_lock = Lock()

def _history_add(entry: dict) -> None:
    with _history_lock:
        _history.appendleft(entry)

def _history_update(token: str, decision: str, result: dict | None) -> None:
    with _history_lock:
        for h in _history:
            if h.get("_token") == token:
                h["decision"] = decision
                h["result"] = result
                break

# ── IST market hours ───────────────────────────────────────────────────────────
def _is_market_open(underlying: str, instrument_config: dict) -> bool:
    now_ist = datetime.now(_IST)
    if now_ist.weekday() >= 5:   # Saturday / Sunday
        return False
    t = now_ist.hour * 60 + now_ist.minute
    cfg = instrument_config.get(underlying.upper(), {})
    if cfg.get("mcx_futures"):
        return (9 * 60) <= t <= (23 * 60 + 30)   # MCX: 09:00–23:30
    return (9 * 60 + 15) <= t <= (15 * 60 + 30)  # NSE: 09:15–15:30

# ── NLU system prompt ──────────────────────────────────────────────────────────
def _build_system_prompt(instrument_config: dict) -> str:
    nfo  = [k for k, v in instrument_config.items() if v.get("exchange") == "NFO"]
    mcx  = [k for k, v in instrument_config.items() if v.get("exchange") == "MCX"]
    all_ = list(instrument_config.keys())
    return f"""You are a trading assistant that translates voice commands into structured JSON orders.
The system trades options on these instruments ONLY: {', '.join(all_)}

NFO instruments (NSE equity derivatives): {', '.join(nfo)} → exchange = "NFO"
MCX instruments (commodity derivatives):  {', '.join(mcx)} → exchange = "MCX"

Return ONLY a valid JSON object with this exact schema (no markdown, no explanation):
{{
  "action":          "BUY" | "SELL" | "EXIT_ALL" | "SQUARE_OFF",
  "underlying":      string  (instrument from whitelist above, or best guess),
  "quantity":        integer (default 1 if not stated),
  "option_type":     "CE" | "PE" | null,
  "strike":          float | null  (null = ATM; system will find closest to delta 0.65),
  "target_delta":    0.65,
  "options_mode":    true,
  "exchange":        "NFO" | "MCX",
  "current_price":   0,
  "target":          null,
  "stoploss":        null,
  "confidence":      float 0.0–1.0,
  "uncertain_fields": [list of field names you had to assume],
  "action_type":     "entry" | "exit_all" | "square_off"
}}

Translation rules:
• "call" / "CE"  → option_type = "CE"
• "put"  / "PE"  → option_type = "PE"
• Bullish (buy call, long call, buy CE)  → action = "BUY"
• Bearish (buy put, long put, buy PE)    → action = "SELL"  (system maps SELL → PE entry)
• "one lot" / "1 lot" → quantity=1; "two lots" → 2; etc. Default=1 if not stated
• "ATM" or no strike → strike = null  (system finds ATM via delta 0.65)
• "exit all", "square off all [X]"   → action="EXIT_ALL",   action_type="exit_all"
• "square off [instrument]"          → action="SQUARE_OFF", action_type="square_off"
• If instrument NOT in whitelist → still parse best guess, set confidence=0.0
• confidence < 0.85 when: instrument ambiguous, quantity not stated, action unclear
• List EVERY field you assumed or guessed in uncertain_fields
• exchange is derived from the instrument — do not ask the user

Examples:

Input: "Buy one lot of BANKNIFTY ATM call"
{{"action":"BUY","underlying":"BANKNIFTY","quantity":1,"option_type":"CE","strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.98,"uncertain_fields":[],"action_type":"entry"}}

Input: "Sell two lots NIFTY 25000 put"
{{"action":"SELL","underlying":"NIFTY","quantity":2,"option_type":"PE","strike":25000.0,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.97,"uncertain_fields":[],"action_type":"entry"}}

Input: "Buy CRUDEOILM call, 3 lots"
{{"action":"BUY","underlying":"CRUDEOILM","quantity":3,"option_type":"CE","strike":null,"target_delta":0.65,"options_mode":true,"exchange":"MCX","current_price":0,"target":null,"stoploss":null,"confidence":0.95,"uncertain_fields":[],"action_type":"entry"}}

Input: "Exit all BANKNIFTY positions"
{{"action":"EXIT_ALL","underlying":"BANKNIFTY","quantity":0,"option_type":null,"strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.96,"uncertain_fields":[],"action_type":"exit_all"}}

Input: "Square off NIFTY"
{{"action":"SQUARE_OFF","underlying":"NIFTY","quantity":0,"option_type":null,"strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.93,"uncertain_fields":[],"action_type":"square_off"}}

Input: "BANKNIFTY call" (no action, no quantity)
{{"action":"BUY","underlying":"BANKNIFTY","quantity":1,"option_type":"CE","strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.52,"uncertain_fields":["action","quantity"],"action_type":"entry"}}"""


# ── Claude NLU call ────────────────────────────────────────────────────────────
def _call_nlu(transcript: str, instrument_config: dict) -> dict:
    api_key = os.getenv(ANTHROPIC_API_KEY_ENV, "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=_build_system_prompt(instrument_config),
        messages=[{"role": "user", "content": transcript.strip()}],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown code fences if the model wraps the JSON
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── OpenAI Whisper transcription ───────────────────────────────────────────────
def _transcribe_audio(audio_bytes: bytes, filename: str) -> str:
    api_key = os.getenv(OPENAI_API_KEY_ENV, "")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set — send pre-transcribed text via the 'text' field instead"
        )
    import io
    import openai
    client = openai.OpenAI(api_key=api_key)
    buf = io.BytesIO(audio_bytes)
    buf.name = filename or "audio.wav"
    result = client.audio.transcriptions.create(model="whisper-1", file=buf, language="en")
    return result.text


# ── Human-readable confirmation summary ───────────────────────────────────────
def _build_summary(entry: dict, instrument_config: dict) -> str:
    action      = entry.get("action", "?")
    underlying  = entry.get("underlying", "?")
    qty         = entry.get("quantity", 1)
    opt_type    = entry.get("option_type") or entry.get("_option_type")
    strike      = entry.get("strike") or entry.get("_strike")
    confidence  = float(entry.get("_confidence", entry.get("confidence", 0)))
    action_type = entry.get("action_type", "entry")
    uncertain   = entry.get("uncertain_fields", entry.get("_uncertain_fields", []))

    cfg      = instrument_config.get(underlying, {})
    lot_size = cfg.get("lot_size", "?")

    if action_type in ("exit_all", "square_off"):
        summary = f"EXIT ALL {underlying} positions — are you sure?"
    else:
        strike_str = f"strike {int(strike)}" if strike else "ATM (delta 0.65 — assumed)"
        opt_str    = f"{opt_type} " if opt_type else ""
        summary    = (
            f"You want to {action} {qty} lot(s) of {underlying} "
            f"{opt_str}[{strike_str}, lot size {lot_size}] — confirm?"
        )

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


# ── Blueprint factory ──────────────────────────────────────────────────────────
def create_voice_blueprint(
    execute_order_fn,
    exit_position_fn,
    instrument_config: dict,
) -> Blueprint:
    """
    Returns a Flask Blueprint with all voice endpoints wired up.
    Call from webhook_server.py after defining execute_order and exit_position.
    """
    bp      = Blueprint("voice", __name__)
    ALLOWED = set(instrument_config.keys())

    # ── shared gate ───────────────────────────────────────────────────────────
    def _gate_voice():
        if not is_voice_enabled():
            return jsonify({
                "error": "Voice channel is disabled.",
                "hint" : "Enable via: POST /admin/voice/toggle  {\"enabled\": true}",
            }), 403
        if not _check_voice_auth():
            _audit.warning(f"UNAUTHORIZED ip:{request.remote_addr}")
            return jsonify({"error": "Missing or invalid X-Voice-Auth-Token header"}), 401
        return None

    # ══════════════════════════════════════════════════════════════════════════
    # POST /voice/transcribe
    # ══════════════════════════════════════════════════════════════════════════
    @bp.route("/voice/transcribe", methods=["POST"])
    def transcribe():
        err = _gate_voice()
        if err:
            return err

        token_hdr = request.headers.get("X-Voice-Auth-Token", "anon")
        if not _rate_check(token_hdr):
            _audit.warning(f"RATE_LIMIT ip:{request.remote_addr}")
            return jsonify({"error": "Rate limit exceeded (30 requests/min)"}), 429

        source_ip  = request.remote_addr
        transcript = None

        # Path 1: pre-transcribed text (preferred — no OPENAI_API_KEY needed)
        if request.is_json:
            transcript = (request.json or {}).get("text", "").strip()
        if not transcript and request.form.get("text"):
            transcript = request.form["text"].strip()

        # Path 2: audio file → Whisper
        if not transcript:
            audio_file = request.files.get("audio")
            if not audio_file:
                return jsonify({"error": "Provide 'text' field (pre-transcribed) or 'audio' file"}), 400
            mode = os.getenv(TRANSCRIPTION_MODE_ENV, "text_only")
            if mode == "text_only":
                return jsonify({
                    "error": (
                        "Audio transcription is disabled. "
                        "Set TRANSCRIPTION_MODE=whisper and OPENAI_API_KEY, "
                        "or send pre-transcribed text via the 'text' field."
                    )
                }), 400
            try:
                audio_bytes = audio_file.read()
                if len(audio_bytes) == 0:
                    return jsonify({"error": "Empty audio file"}), 400
                transcript = _transcribe_audio(audio_bytes, audio_file.filename or "audio.wav")
            except Exception as exc:
                _audit.error(f"TRANSCRIBE_FAILED ip:{source_ip} error:{exc}")
                return jsonify({"error": f"Transcription failed: {exc}"}), 500

        if not transcript:
            return jsonify({"error": "Empty transcript — nothing to parse"}), 400

        _audit.info(f"TRANSCRIPT ip:{source_ip} text:{transcript!r}")

        # NLU
        try:
            nlu = _call_nlu(transcript, instrument_config)
        except json.JSONDecodeError as exc:
            _audit.error(f"NLU_BAD_JSON ip:{source_ip} error:{exc}")
            return jsonify({"error": f"NLU returned invalid JSON: {exc}"}), 500
        except Exception as exc:
            _audit.error(f"NLU_FAILED ip:{source_ip} transcript:{transcript!r} error:{exc}")
            return jsonify({"error": f"NLU call failed: {exc}"}), 500

        confidence  = float(nlu.get("confidence", 0))
        action      = nlu.get("action", "")
        underlying  = nlu.get("underlying", "")
        quantity    = int(nlu.get("quantity", 1))
        action_type = nlu.get("action_type", "entry")

        # Instrument whitelist
        if underlying not in ALLOWED:
            _audit.warning(
                f"REJECTED_INSTRUMENT ip:{source_ip} instrument:{underlying!r}"
            )
            return jsonify({
                "error"  : f"Instrument '{underlying}' not in whitelist: {sorted(ALLOWED)}",
                "parsed" : nlu,
            }), 400

        # Market hours (only for new entries, not exits)
        if action_type == "entry":
            if not _is_market_open(underlying, instrument_config):
                after_hours = _load_config().get("after_hours_mode", "reject")
                if after_hours == "reject":
                    _audit.warning(
                        f"REJECTED_AFTER_HOURS ip:{source_ip} underlying:{underlying}"
                    )
                    return jsonify({
                        "error"  : f"Market is closed for {underlying}. Voice orders rejected outside trading hours.",
                        "parsed" : nlu,
                    }), 400

            # Quantity guard
            if quantity > MAX_VOICE_LOTS:
                _audit.warning(
                    f"REJECTED_QTY ip:{source_ip} qty:{quantity} max:{MAX_VOICE_LOTS}"
                )
                return jsonify({
                    "error"  : (
                        f"Quantity {quantity} exceeds MAX_VOICE_LOTS={MAX_VOICE_LOTS}. "
                        "Place this order manually."
                    ),
                    "parsed" : nlu,
                }), 400

        # Voice dedup (10-second window)
        dedup_key = f"{action}|{underlying}|{quantity}"
        if _voice_is_duplicate(dedup_key):
            return jsonify({
                "error"  : f"Duplicate voice command (identical within {VOICE_DEDUP_SECONDS}s). Wait and retry.",
                "parsed" : nlu,
            }), 429

        # Build pending entry
        conf_token     = str(uuid.uuid4())
        low_confidence = confidence < 0.85
        is_exit        = action_type in ("exit_all", "square_off")

        pending_entry = {
            # Internal fields (prefixed _) — stripped before returning to client
            "_expires"         : time.monotonic() + CONFIRM_TTL_SECONDS,
            "_token"           : conf_token,
            "_source_ip"       : source_ip,
            "_transcript"      : transcript,
            "_confidence"      : confidence,
            "_low_confidence"  : low_confidence,
            "_is_exit"         : is_exit,
            "_confirm_step"    : 1,
            "_option_type"     : nlu.get("option_type"),
            "_strike"          : nlu.get("strike"),
            "_uncertain_fields": nlu.get("uncertain_fields", []),
            # Order fields — passed directly to execute_order_fn / exit_position_fn
            "action_type"      : action_type,
            "action"           : action,
            "underlying"       : underlying,
            "quantity"         : quantity,
            "options_mode"     : True,
            "target_delta"     : float(nlu.get("target_delta", 0.65)),
            "exchange"         : nlu.get("exchange", "NFO"),
            "current_price"    : 0,
            "target"           : None,
            "stoploss"         : None,
            "symbol"           : "",
        }
        _store_pending(conf_token, pending_entry)

        summary = _build_summary(pending_entry, instrument_config)

        _audit.info(
            f"PENDING token:{conf_token} ip:{source_ip} action:{action} "
            f"underlying:{underlying} qty:{quantity} confidence:{confidence:.2f} "
            f"low_conf:{low_confidence} is_exit:{is_exit}"
        )

        # Record in history
        _history_add({
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

        return jsonify({
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
        }), 200

    # ══════════════════════════════════════════════════════════════════════════
    # POST /voice/confirm
    # ══════════════════════════════════════════════════════════════════════════
    @bp.route("/voice/confirm", methods=["POST"])
    def confirm():
        err = _gate_voice()
        if err:
            return err

        body     = request.json or {}
        token    = body.get("confirmation_token", "")
        approved = bool(body.get("approved", False))

        if not token:
            return jsonify({"error": "confirmation_token required"}), 400

        entry = _get_pending(token)
        if entry is None:
            _audit.warning(f"EXPIRED_TOKEN token:{token} ip:{request.remote_addr}")
            return jsonify({"error": "Token not found or expired (60 s TTL)"}), 404

        if not approved:
            _pop_pending(token)
            _audit.info(f"REJECTED token:{token} ip:{request.remote_addr}")
            _history_update(token, "rejected", None)
            return jsonify({"status": "rejected", "message": "Order cancelled by user"}), 200

        # Re-check kill switch (channel may have been toggled off between parse and confirm)
        if not is_voice_enabled():
            _pop_pending(token)
            _audit.warning(f"CHANNEL_DISABLED_AT_CONFIRM token:{token}")
            _history_update(token, "channel_disabled", None)
            return jsonify({
                "error": "Voice channel was disabled after this order was parsed — order cancelled."
            }), 403

        low_conf     = entry.get("_low_confidence", False)
        is_exit      = entry.get("_is_exit", False)
        confirm_step = entry.get("_confirm_step", 1)
        action_type  = entry.get("action_type", "entry")
        underlying   = entry.get("underlying", "")

        # Gate 1: low-confidence override
        if low_conf and not body.get("low_confidence_override", False):
            return jsonify({
                "status"             : "low_confidence_confirmation_required",
                "message"            : (
                    f"Confidence is {entry['_confidence']:.0%} — below the 85% threshold. "
                    "Carefully review the parsed order, then resend with "
                    "\"low_confidence_override\": true to proceed."
                ),
                "confirmation_token" : token,
                "summary"            : _build_summary(entry, instrument_config),
                "parsed_order"       : {
                    k: v for k, v in entry.items() if not k.startswith("_")
                    and k != "action_type"
                },
            }), 200

        # Gate 2: double-confirm for EXIT_ALL / SQUARE_OFF
        if is_exit and confirm_step == 1:
            entry["_confirm_step"] = 2
            with _pending_lock:
                _pending[token] = entry
            _audit.info(f"DOUBLE_CONFIRM_STEP1 token:{token} ip:{request.remote_addr}")
            return jsonify({
                "status"             : "second_confirmation_required",
                "message"            : (
                    f"⚠ SECOND CONFIRMATION REQUIRED — "
                    f"You are about to exit ALL open {underlying} positions. "
                    "This cannot be undone. Send approved: true again to confirm."
                ),
                "confirmation_token" : token,
            }), 200

        # All gates passed — execute
        _pop_pending(token)
        source_ip = entry.get("_source_ip", "unknown")

        try:
            if action_type in ("exit_all", "square_off"):
                result, status_code = exit_position_fn(underlying, source="voice_manual")
            else:
                order_data = {
                    k: v for k, v in entry.items()
                    if not k.startswith("_") and k != "action_type"
                }
                result, status_code = execute_order_fn(order_data, source="voice_manual")
        except Exception as exc:
            _audit.error(
                f"EXECUTE_FAILED token:{token} ip:{source_ip} "
                f"underlying:{underlying} error:{exc}"
            )
            _history_update(token, "execute_exception", {"error": str(exc)})
            return jsonify({"error": f"Order execution raised an exception: {exc}"}), 500

        decision = "approved_executed" if status_code < 400 else "approved_failed"
        _audit.info(
            f"CONFIRMED token:{token} ip:{source_ip} action_type:{action_type} "
            f"underlying:{underlying} decision:{decision} result:{json.dumps(result)}"
        )
        _history_update(token, decision, result)

        return jsonify({
            "status"     : decision,
            "token"      : token,
            "result"     : result,
            "transcript" : entry.get("_transcript"),
            "confidence" : entry.get("_confidence"),
        }), status_code

    # ══════════════════════════════════════════════════════════════════════════
    # POST /voice/cancel
    # ══════════════════════════════════════════════════════════════════════════
    @bp.route("/voice/cancel", methods=["POST"])
    def cancel():
        err = _gate_voice()
        if err:
            return err

        body  = request.json or {}
        token = body.get("confirmation_token", "")
        if not token:
            return jsonify({"error": "confirmation_token required"}), 400

        entry = _pop_pending(token)
        if entry is None:
            return jsonify({"error": "Token not found or already expired"}), 404

        _audit.info(f"CANCELLED token:{token} ip:{request.remote_addr}")
        _history_update(token, "cancelled", None)
        return jsonify({"status": "cancelled", "token": token}), 200

    # ══════════════════════════════════════════════════════════════════════════
    # GET /voice/pending
    # ══════════════════════════════════════════════════════════════════════════
    @bp.route("/voice/pending", methods=["GET"])
    def pending():
        err = _gate_voice()
        if err:
            return err

        _expire_pending()
        now = time.monotonic()
        with _pending_lock:
            items = [
                {
                    "token"          : v["_token"],
                    "action"         : v.get("action"),
                    "action_type"    : v.get("action_type"),
                    "underlying"     : v.get("underlying"),
                    "quantity"       : v.get("quantity"),
                    "confidence"     : v.get("_confidence"),
                    "low_confidence" : v.get("_low_confidence"),
                    "is_exit"        : v.get("_is_exit"),
                    "confirm_step"   : v.get("_confirm_step"),
                    "transcript"     : v.get("_transcript"),
                    "expires_in_sec" : round(v["_expires"] - now),
                    "source_ip"      : v.get("_source_ip"),
                }
                for v in _pending.values()
            ]
        return jsonify({"pending_orders": items, "count": len(items)}), 200

    # ══════════════════════════════════════════════════════════════════════════
    # GET /admin/voice/status
    # ══════════════════════════════════════════════════════════════════════════
    @bp.route("/admin/voice/status", methods=["GET"])
    def admin_status():
        if not _check_admin_auth():
            return jsonify({"error": "Missing or invalid X-Admin-Token header"}), 401
        cfg = _load_config()
        return jsonify({
            "voice_channel_enabled" : cfg.get("voice_channel_enabled", False),
            "after_hours_mode"      : cfg.get("after_hours_mode", "reject"),
            "max_voice_lots"        : MAX_VOICE_LOTS,
            "pending_orders"        : len(_pending),
            "confirm_ttl_seconds"   : CONFIRM_TTL_SECONDS,
            "voice_dedup_seconds"   : VOICE_DEDUP_SECONDS,
            "config_path"           : CONFIG_PATH,
            "transcription_mode"    : os.getenv(TRANSCRIPTION_MODE_ENV, "text_only"),
            "anthropic_key_set"     : bool(os.getenv(ANTHROPIC_API_KEY_ENV)),
            "openai_key_set"        : bool(os.getenv(OPENAI_API_KEY_ENV)),
        }), 200

    # ══════════════════════════════════════════════════════════════════════════
    # POST /admin/voice/toggle
    # ══════════════════════════════════════════════════════════════════════════
    @bp.route("/admin/voice/toggle", methods=["POST"])
    def admin_toggle():
        if not _check_admin_auth():
            return jsonify({"error": "Missing or invalid X-Admin-Token header"}), 401

        body = request.json or {}
        cfg  = _load_config()

        if "enabled" in body:
            new_state = bool(body["enabled"])
        else:
            new_state = not cfg.get("voice_channel_enabled", False)

        cfg["voice_channel_enabled"] = new_state
        _save_config(cfg)

        state_str = "ENABLED" if new_state else "DISABLED"
        _audit.info(f"TOGGLE voice_channel={state_str} from {request.remote_addr}")

        return jsonify({
            "status"                : "ok",
            "voice_channel_enabled" : new_state,
            "message"               : f"Voice channel {state_str}",
        }), 200

    # ══════════════════════════════════════════════════════════════════════════
    # GET /admin/voice/history
    # ══════════════════════════════════════════════════════════════════════════
    @bp.route("/admin/voice/history", methods=["GET"])
    def admin_history():
        if not _check_admin_auth():
            return jsonify({"error": "Missing or invalid X-Admin-Token header"}), 401

        limit = min(int(request.args.get("limit", 20)), 100)
        today = datetime.now(_IST).date().isoformat()

        with _history_lock:
            entries = list(_history)[:limit]
            orders_today = sum(
                1 for h in _history
                if h.get("ts", "").startswith(today)
                and h.get("decision") in ("approved_executed", "approved_failed")
            )

        safe = [{k: v for k, v in h.items() if k != "_token"} for h in entries]
        return jsonify({
            "history"           : safe,
            "count"             : len(safe),
            "voice_orders_today": orders_today,
        }), 200

    return bp
