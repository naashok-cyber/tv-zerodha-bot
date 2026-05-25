# Voice Trading Channel — tv-zerodha-bot

Ported from `trading-bot-voice/` (Flask, HTTP) to `app/routes/` (FastAPI, HTTPS).
All endpoints are reachable at `https://naashshla.duckdns.org/voice/*` and
`https://naashshla.duckdns.org/admin/voice/*`.

---

## Required environment variables

| Variable | Required | Description |
|---|---|---|
| `VOICE_AUTH_TOKEN` | **Yes** | Secret token for `/voice/*` endpoints. Set in docker-compose or systemd unit. |
| `ADMIN_AUTH_TOKEN` | **Yes** | Secret token for `/admin/voice/*` endpoints. |
| `ANTHROPIC_API_KEY` | **Yes** | Claude API key for NLU parsing. |
| `OPENAI_API_KEY` | No | Only needed for `TRANSCRIPTION_MODE=whisper`. |
| `TRANSCRIPTION_MODE` | No | `text_only` (default) or `whisper`. Whisper requires OPENAI_API_KEY. |
| `VOICE_CONFIG_PATH` | No | Path for persistent voice channel config. Default: `data/voice_config.json`. |

**Generate fresh tokens for deployment** (do NOT reuse tokens from the trading-bot VM):
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Optional settings (configure in `.env` or docker-compose env)

| Variable | Default | Description |
|---|---|---|
| `VOICE_NLU_MODEL` | `claude-sonnet-4-6` | Anthropic model used for NLU parsing. |
| `VOICE_ALLOWED_INSTRUMENTS` | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, CRUDEOIL, CRUDEOILM, GOLD, SILVER | Instrument whitelist (JSON list). |
| `VOICE_NFO_INSTRUMENTS` | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX | NSE derivatives for NLU prompt. |
| `VOICE_MCX_INSTRUMENTS` | CRUDEOIL, CRUDEOILM, GOLD, SILVER | MCX commodities for NLU prompt. |
| `VOICE_MAX_LOTS` | `5` | Maximum lots allowed per voice order (sanity guard). |
| `VOICE_CONFIRM_TTL_SECONDS` | `60` | Pending order expiry window. |
| `VOICE_RATE_LIMIT` | `30` | Max requests per 60-second window per token. |

---

## Endpoints

### User endpoints — `X-Voice-Auth-Token` required

#### `POST /voice/transcribe`
Parse a voice command transcript → pending order.

```bash
curl -X POST https://naashshla.duckdns.org/voice/transcribe \
  -H "Content-Type: application/json" \
  -H "X-Voice-Auth-Token: $VOICE_AUTH_TOKEN" \
  -d '{"text": "Buy one lot of BANKNIFTY ATM call"}'
```

Response:
```json
{
  "status": "pending_confirmation",
  "confirmation_token": "uuid...",
  "summary": "You want to BUY 1 lot(s) of BANKNIFTY CE [ATM (delta 0.65 — assumed)] — confirm?",
  "confidence": 0.98,
  "low_confidence": false,
  "double_confirm_required": false,
  "expires_in_seconds": 60
}
```

#### `POST /voice/confirm`
Approve a pending order. Requires `confirmation_token` from `/voice/transcribe`.

```bash
curl -X POST https://naashshla.duckdns.org/voice/confirm \
  -H "Content-Type: application/json" \
  -H "X-Voice-Auth-Token: $VOICE_AUTH_TOKEN" \
  -d '{"confirmation_token": "uuid...", "approved": true}'
```

- Low-confidence orders (< 0.85): resend with `"low_confidence_override": true`
- EXIT_ALL / SQUARE_OFF: requires two separate confirms (step 1 → step 2)
- Reject with `"approved": false` to cancel

#### `POST /voice/cancel`
Cancel a pending order without executing.

```bash
curl -X POST https://naashshla.duckdns.org/voice/cancel \
  -H "Content-Type: application/json" \
  -H "X-Voice-Auth-Token: $VOICE_AUTH_TOKEN" \
  -d '{"confirmation_token": "uuid..."}'
```

#### `GET /voice/pending`
List all active pending orders (not yet confirmed or expired).

```bash
curl https://naashshla.duckdns.org/voice/pending \
  -H "X-Voice-Auth-Token: $VOICE_AUTH_TOKEN"
```

---

### Admin endpoints — `X-Admin-Token` required

#### `GET /admin/voice/status`
Channel config + runtime counters.

```bash
curl https://naashshla.duckdns.org/admin/voice/status \
  -H "X-Admin-Token: $ADMIN_AUTH_TOKEN"
```

#### `POST /admin/voice/toggle`
Enable or disable the voice channel (kill switch).

```bash
# Enable
curl -X POST https://naashshla.duckdns.org/admin/voice/toggle \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: $ADMIN_AUTH_TOKEN" \
  -d '{"enabled": true}'

# Toggle (flip current state)
curl -X POST https://naashshla.duckdns.org/admin/voice/toggle \
  -H "X-Admin-Token: $ADMIN_AUTH_TOKEN" \
  -d '{}'
```

#### `GET /admin/voice/history`
Last N voice commands with decisions.

```bash
curl "https://naashshla.duckdns.org/admin/voice/history?limit=20" \
  -H "X-Admin-Token: $ADMIN_AUTH_TOKEN"
```

---

## iOS Shortcut integration

Use these endpoints from Shortcuts → Get Contents of URL:

| Action | Method | URL | Headers |
|---|---|---|---|
| Transcribe | POST | `https://naashshla.duckdns.org/voice/transcribe` | `X-Voice-Auth-Token: <token>` |
| Confirm | POST | `https://naashshla.duckdns.org/voice/confirm` | `X-Voice-Auth-Token: <token>` |
| Cancel | POST | `https://naashshla.duckdns.org/voice/cancel` | `X-Voice-Auth-Token: <token>` |
| Status | GET | `https://naashshla.duckdns.org/admin/voice/status` | `X-Admin-Token: <token>` |

**Suggested flow** in Shortcuts:
1. Dictate text → pass to `/voice/transcribe`
2. Show summary to user (alert or notification)
3. Ask "Confirm?" → if Yes, POST `/voice/confirm` with `{"confirmation_token": ..., "approved": true}`
4. Show result

---

## Audit log

Located at `logs/voice_audit.log` (10 MB × 5 files, RotatingFileHandler).
Events logged:
- `TRANSCRIPT` — received text
- `PENDING` — created pending order
- `CONFIRMED` — order executed
- `REJECTED` — user declined
- `CANCELLED` — user cancelled
- `EXPIRED_TOKEN` — confirm attempt on expired token
- `CHANNEL_DISABLED_AT_CONFIRM` — kill switch toggled between parse and confirm
- `RATE_LIMIT` — request throttled
- `UNAUTHORIZED` — bad token
- `TOGGLE` — kill switch state change

---

## Architecture notes

- Voice channel is **disabled by default**. Enable via `POST /admin/voice/toggle {"enabled": true}`.
- Voice config persists in `data/voice_config.json` using atomic writes (write-to-tmp + os.replace).
- Pending orders are in-memory with 60 s TTL. Server restart clears pending orders.
- BUY/SELL voice orders flow through the same `_process_alert` pipeline as TradingView webhooks (risk gates, strike selection, GTT placement). Quantity specified by voice is used as a sanity guard (MAX_VOICE_LOTS) but actual lot sizing follows the risk engine.
- EXIT_ALL / SQUARE_OFF bypass `_process_alert` and exit all positions for the specified underlying by matching `Position.underlying`, cancelling active GTTs, and placing market square-off orders.
- The voice channel shares the same HTTPS endpoint as the main bot — no new infra needed.

---

## Migration from trading-bot VM

The voice functionality was migrated from:
- **Source**: `trading-bot` GCP VM → `/home/naashok/trading-bot/voice_handler.py` (Flask Blueprint)
- **Destination**: `tv-zerodha-bot` → `app/routes/voice.py` + `app/routes/admin_voice.py` (FastAPI APIRouter)

The trading-bot Flask voice server's systemd unit was stopped on 2026-05-25.
The trading-bot VM is kept as rollback for 1 week (until 2026-06-01).

To roll back:
1. Revert `docker-compose.yml` env changes on `tv-zerodha-bot`
2. Restart the FastAPI container with the previous image tag
3. Restart `trading-bot.service` on the trading-bot VM
