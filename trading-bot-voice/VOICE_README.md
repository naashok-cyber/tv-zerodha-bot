# Voice Command Trading Channel

## Security Notice

> **The existing `/webhook` endpoint has no authentication.** TradingView does not send custom headers by default, so this was left open. Before exposing the server publicly, add an `X-TV-Secret` header check to the `webhook()` route and configure it in TradingView's webhook → Headers field. This is a known follow-up item.

---

## Overview

The voice channel adds a second order-entry path alongside TradingView alerts. A voice client (mobile app, browser, or curl) submits audio or text; Claude NLU parses the intent; a human confirmation step is required before any order is placed.

```
Client → POST /voice/transcribe  →  pending order (60s TTL)
Client → POST /voice/confirm     →  execute_order() / exit_position()
Client → POST /voice/cancel      →  discard pending order
Client → GET  /voice/pending     →  inspect pending order
Admin  → GET  /admin/voice/status / POST /admin/voice/toggle
```

---

## Environment Variables

Set these on the server before starting `webhook_server.py`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `VOICE_AUTH_TOKEN` | Yes | — | Bearer token clients must send in `X-Voice-Auth-Token` header |
| `ADMIN_AUTH_TOKEN` | Yes | — | Bearer token for `/admin/*` endpoints (`X-Admin-Token`) |
| `ANTHROPIC_API_KEY` | Yes | — | API key for Claude NLU (model: `claude-sonnet-4-5`) |
| `OPENAI_API_KEY` | No | — | Required only when `TRANSCRIPTION_MODE=whisper` |
| `TRANSCRIPTION_MODE` | No | `text` | `text` = client sends transcript; `whisper` = client sends audio file |
| `MAX_VOICE_LOTS` | No | `5` | Hard cap on lot size accepted via voice |

### Example (add to `~/.bashrc` or `/etc/environment`)

```bash
export VOICE_AUTH_TOKEN="your-secret-voice-token"
export ADMIN_AUTH_TOKEN="your-secret-admin-token"
export ANTHROPIC_API_KEY="sk-ant-..."
export MAX_VOICE_LOTS=5
```

---

## Enabling the Channel

The channel is **disabled by default** (`config.json` → `voice_channel_enabled: false`). Enable it via the admin API or the dashboard:

```bash
curl -s -X POST https://<host>/admin/voice/toggle \
  -H "X-Admin-Token: $ADMIN_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

To disable:

```bash
curl -s -X POST https://<host>/admin/voice/toggle \
  -H "X-Admin-Token: $ADMIN_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

---

## JSON Schemas

### POST /voice/transcribe (text mode)

```json
{
  "transcript": "buy 2 lots Bank Nifty",
  "source_ip": "optional-override"
}
```

### POST /voice/transcribe (whisper mode — multipart)

```
Content-Type: multipart/form-data
  audio: <file>   (wav / mp3 / m4a / ogg, max 25MB)
```

### POST /voice/confirm

```json
{
  "token": "<pending_token>",
  "low_confidence_override": false
}
```

`low_confidence_override` must be `true` when Claude confidence < 0.85.  
EXIT_ALL / SQUARE_OFF commands require **two** confirmations (send the same payload twice).

### POST /voice/cancel

```json
{
  "token": "<pending_token>"
}
```

### GET /voice/pending

No body. Returns the pending order for the caller's token, if any.

### GET /admin/voice/status

No body. Returns:

```json
{
  "voice_channel_enabled": false,
  "pending_orders": 0,
  "voice_orders_today": 3,
  "max_voice_lots": 5,
  "after_hours_mode": "reject"
}
```

### POST /admin/voice/toggle

```json
{ "enabled": true }
```

### GET /admin/voice/history?limit=20

Returns the last N voice commands (default 20, max 100).

---

## curl Examples

Replace `HOST`, `VOICE_TOKEN`, and `ADMIN_TOKEN` below.

```bash
HOST="http://35.200.151.39:5000"
VOICE_TOKEN="your-secret-voice-token"
ADMIN_TOKEN="your-secret-admin-token"
```

### 1. Submit a voice command (text mode)

```bash
curl -s -X POST "$HOST/voice/transcribe" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"transcript": "buy 2 lots Bank Nifty"}' | jq .
```

Expected response (pending):

```json
{
  "status": "pending_confirmation",
  "token": "abc123...",
  "parsed": {
    "action": "BUY",
    "underlying": "BANKNIFTY",
    "quantity": 2,
    "confidence": 0.97
  },
  "message": "Confirm: BUY BANKNIFTY ×2?"
}
```

### 2. Confirm the order

```bash
curl -s -X POST "$HOST/voice/confirm" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token": "abc123..."}' | jq .
```

### 3. Cancel the order

```bash
curl -s -X POST "$HOST/voice/cancel" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token": "abc123..."}' | jq .
```

### 4. Check pending orders

```bash
curl -s "$HOST/voice/pending" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" | jq .
```

### 5. Low-confidence override

```bash
# If /transcribe returns low_confidence: true, confirm with override:
curl -s -X POST "$HOST/voice/confirm" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token": "abc123...", "low_confidence_override": true}' | jq .
```

### 6. Exit all positions (double-confirm required)

```bash
curl -s -X POST "$HOST/voice/transcribe" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"transcript": "exit all Bank Nifty positions"}' | jq .

# First confirm:
curl -s -X POST "$HOST/voice/confirm" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token": "abc123..."}' | jq .
# Returns: {"status": "second_confirmation_required", ...}

# Second confirm:
curl -s -X POST "$HOST/voice/confirm" \
  -H "X-Voice-Auth-Token: $VOICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token": "abc123..."}' | jq .
```

### 7. Voice channel status (admin)

```bash
curl -s "$HOST/admin/voice/status" \
  -H "X-Admin-Token: $ADMIN_TOKEN" | jq .
```

### 8. Last 20 voice commands (admin)

```bash
curl -s "$HOST/admin/voice/history?limit=20" \
  -H "X-Admin-Token: $ADMIN_TOKEN" | jq .
```

---

## Deploy Command

Run from your local machine. Replace paths if your staging directory differs.

```bash
ZONE="asia-south1-a"
PROJECT="trading-bot-493918"
REMOTE="trading-bot:/home/naashok/trading-bot"
LOCAL="C:\Users\srias\tv-zerodha-bot\trading-bot-voice"

# Copy all files
gcloud compute scp "$LOCAL/voice_handler.py"  "$REMOTE/voice_handler.py"  --zone=$ZONE --project=$PROJECT
gcloud compute scp "$LOCAL/webhook_server.py" "$REMOTE/webhook_server.py" --zone=$ZONE --project=$PROJECT
gcloud compute scp "$LOCAL/dashboard.html"    "$REMOTE/dashboard.html"    --zone=$ZONE --project=$PROJECT
gcloud compute scp "$LOCAL/requirements.txt"  "$REMOTE/requirements.txt"  --zone=$ZONE --project=$PROJECT
gcloud compute scp "$LOCAL/config.json"       "$REMOTE/config.json"       --zone=$ZONE --project=$PROJECT

# Install new dependencies
gcloud compute ssh trading-bot --zone=$ZONE --project=$PROJECT \
  --command="cd /home/naashok/trading-bot && source venv/bin/activate && pip install -r requirements.txt"

# Restart the server (adjust if using systemd)
gcloud compute ssh trading-bot --zone=$ZONE --project=$PROJECT \
  --command="pkill -f webhook_server.py; sleep 1; cd /home/naashok/trading-bot && source venv/bin/activate && nohup python webhook_server.py > logs/server.log 2>&1 &"
```

---

## Supported Instruments (Whitelist)

Only instruments in `INSTRUMENT_CONFIG` (defined in `webhook_server.py`) are accepted. Current whitelist:

- `BANKNIFTY`
- `NIFTY`
- `FINNIFTY`
- `MIDCPNIFTY`
- `SENSEX`

---

## Audit Log

All voice commands are written to `logs/voice_audit.log` (RotatingFileHandler, 10 MB × 5 files).

Each line is JSON:

```json
{
  "ts": "2025-01-01T09:15:00+05:30",
  "source_ip": "1.2.3.4",
  "transcript": "buy 2 lots Bank Nifty",
  "parsed": {"action": "BUY", "underlying": "BANKNIFTY", "quantity": 2},
  "confidence": 0.97,
  "decision": "approved_executed",
  "result": {"option_symbol": "BANKNIFTY25JAN45000CE", "entry_price": 142.5}
}
```

`decision` values: `pending`, `approved_executed`, `approved_failed`, `rejected`, `cancelled`, `expired`, `channel_disabled`, `execute_exception`, `low_confidence_confirmation_required`, `second_confirmation_required`
