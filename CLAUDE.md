# tv-zerodha-bot — CLAUDE.md

Production algorithmic trading bot: TradingView webhooks + voice commands → Zerodha Kite options/futures orders with delta-based strike selection, GTT OCO exits, and trailing SL.

---

## Stack

| Layer | Library |
|-------|---------|
| API | FastAPI + uvicorn (ASGI) |
| Broker | KiteConnect 5.2 (REST + WebSocket) |
| DB | SQLAlchemy 2.0 + SQLite (`data/bot.db`) |
| Validation | Pydantic v2 / pydantic-settings |
| Scheduling | APScheduler 3 + custom `_PreciseTimer` threads |
| Voice NLU | Anthropic Claude API (`claude-sonnet-4-6`) |
| Audio | OpenAI Whisper (optional, `OPENAI_API_KEY`) |
| Greeks | py_vollib + scipy (BSM / Black-76) |
| Crypto | cryptography (PBKDF2 + Fernet, token-at-rest) |
| ADX | stdlib-only `app/adx.py` (Wilder smoothing) |

---

## Directory Map

```
app/
  main.py            — FastAPI app, lifespan, all HTTP routes, signal processor (~3200 lines)
  config.py          — Pydantic BaseSettings from .env; module constants IST/UTC/enums
  state.py           — Runtime overrides (persistent JSON) + session flags (in-memory)
  storage.py         — SQLAlchemy ORM models + init_db()
  orders.py          — place_entry, place_gtt_oco, modify_gtt, cancel_gtt, square_off
  watcher.py         — OrderWatcher: WebSocket fill detection + 7s fallback poll
  trailing.py        — TrailingSlManager: tick-driven SL trailing, 30s GTT throttle
  scheduler.py       — APScheduler jobs + _PreciseTimer; eod squareoff, daily session check
  strike_selector.py — Delta-based strike selection with liquidity guardrails
  greeks.py          — delta_bsm (NSE) + delta_b76 (MCX); IV solvers via scipy
  expiry_resolver.py — Nearest eligible expiry (weekly/monthly, skip-expiry-day logic)
  risk.py            — compute_option_qty, compute_futures_qty, daily loss tracking
  kite_session.py    — OAuth token exchange + PBKDF2/Fernet encryption to disk
  auth.py            — IP allowlist, HMAC webhook verification, per-IP rate limiting
  symbol_mapper.py   — Instrument routing (NSE/NFO/MCX), expiry list, tick rounding
  schemas.py         — AlertPayload (unified webhook schema)
  adx.py             — Wilder-smoothed ADX computation (no deps, pure stdlib)
  auto_login.py      — Headless Kite login: password + TOTP → access_token (PYOTP_AUTO_LOGIN)
  scheduled_straddle.py — Automated short straddle jobs for CRUDEOILM + NATURALGAS
  webauthn_routes.py — Dashboard login (password form → session cookie)
  routes/
    voice.py         — /voice/* endpoints (transcribe, confirm, cancel, pending)
    admin_voice.py   — /admin/voice/toggle
  voice/
    nlu.py           — System prompt builder + Anthropic API call
    executor.py      — execute_voice_entry / execute_voice_straddle / execute_voice_exit
    pending.py       — TTL in-memory pending order store (singleton)
    config.py        — load/save data/voice_config.json
    audit.py         — Voice action audit logging
    straddle.py      — validate_straddle: ATM selection, margin check, spread check
data/               — Runtime: bot.db, access_token.enc, access_token.salt, overrides.json, voice_config.json
tests/              — pytest suite (auth, config, expiry, greeks, orders, risk, voice, etc.)
terraform/          — GCP infrastructure
```

---

## Key HTTP Routes (`app/main.py`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/webhook` | TradingView alert → background `_process_alert` |
| GET | `/control` | Unified dashboard (overrides, quick trade, voice) |
| GET | `/dashboard` | Redirect to /control |
| GET | `/orders` | Open orders page |
| GET | `/positions` | Alias for /dashboard positions tab |
| GET | `/history` | Closed trades page |
| GET | `/gtts` | GTT OCO status page |
| POST | `/trade-mode/toggle` | Toggle BUY_OPTIONS ↔ SELL_OPTIONS |
| POST | `/toggle-paper-mode` | Toggle DRY_RUN override |
| POST | `/toggle-emergency-stop` | Set/clear emergency stop |
| POST | `/toggle-trailing` | Enable/disable trailing SL |
| POST | `/update-risk` | Update capital/lot/loss overrides |
| GET | `/kite/login` | Start Kite OAuth flow |
| GET | `/kite/callback` | Exchange request_token → access_token |
| GET | `/healthz` | Health check (returns session status) |
| POST | `/voice/transcribe` | Voice/text → NLU → pending order |
| POST | `/voice/confirm` | Execute pending order |
| POST | `/voice/cancel` | Discard pending order |
| GET | `/voice/pending` | List pending orders |
| POST | `/admin/voice/toggle` | Enable/disable voice channel |

---

## Signal Processing Pipeline (`_process_alert`)

```
Webhook POST
  → AlertPayload validation (schemas.py)
  → IP check + HMAC check (auth.py)
  → Idempotency check (TTLCache + strategy_id DB dedup)
  → Insert Alert row → 202 Accepted
  → background: _process_alert(alert_id, alert_data, settings)
      → _check_risk_guards()  # abort if any fail
      → Route by action:
          TRAIL       → no-op (trailing is tick-driven)
          EXIT        → cancel_gtt → square_off
          STRADDLE_SHORT → _process_straddle()
          NATURALGAS FUT → resolve_expiry → compute_futures_qty → place_entry MARKET
          EQUITY CNC  → LTP quote → compute_equity_qty → place_entry
          BUY/SELL CE/PE → resolve_expiry → select_strike → compute_option_qty → place_entry
      → EntryFilledEvent (WebSocket or 7s poll)
      → _on_entry_filled() → place_gtt_oco → register TrailingSlManager
      → GttFilledEvent → _on_gtt_filled() → ClosedTrade + PnL
```

---

## Risk Guards (`_check_risk_guards`)

Blocks new entries if any of:
1. `state.is_emergency_stop()` is True
2. `state.get_session_invalid()` is True
3. Daily realized loss >= `MAX_DAILY_LOSS_ABS`
4. Trades today >= `MAX_TRADES_PER_DAY`
5. Open positions >= `MAX_OPEN_POSITIONS`
6. Consecutive SL hits >= `CONSECUTIVE_LOSSES_LIMIT`
7. Current time outside entry window (09:30–15:00 NSE / 09:30–23:00 MCX)

---

## Scheduled Jobs

| Job | Time (IST) | Trigger |
|-----|-----------|---------|
| `auto_login_job` | `KITE_AUTO_LOGIN_TIME` (07:45) | APScheduler cron (if `PYOTP_AUTO_LOGIN=true`) |
| `daily_session_check` | 08:00 | APScheduler cron |
| `refresh_instruments_job` | 08:30 | APScheduler cron |
| `expiry_day_squareoff_job` | 14:00 | APScheduler cron |
| `eod_squareoff_job` NSE | 15:25 | APScheduler cron |
| CRUDEOILM straddle entry | `CRUDEOILM_STRADDLE_TIME` (22:00) | `_PreciseTimer` thread |
| NATURALGAS straddle entry | `NG_STRADDLE_TIME` (22:05) | `_PreciseTimer` thread |
| `eod_squareoff_job` MCX | 23:25 | APScheduler cron |
| Scheduled straddle squareoff | `STRADDLE_SQUAREOFF_TIME` (23:20) | APScheduler cron |

`_PreciseTimer`: dedicated daemon thread that busy-waits the final 10 s before target to fire within ~100 ms. Used for straddle entries where exact timing matters.

---

## Scheduled Straddle (`app/scheduled_straddle.py`)

- **Entry** (`run_scheduled_straddle`):
  1. ADX gate (NATURALGAS only): fetch front-month futures candles → `compute_adx()` → skip if ADX >= `NG_STRADDLE_ADX_THRESHOLD` (22.0)
  2. `validate_straddle()` → ATM CE + PE symbols, margin OK, spread OK
  3. Create Alert row (`strategy_id="sched_straddle_<UNDERLYING>"`)
  4. Call `_process_straddle()` directly (same path as voice straddle)

- **Squareoff** (`squareoff_scheduled_straddles`):
  - Targets `CRUDEOILM` + `NATURALGAS` open CE/PE positions on MCX
  - Cancels GTT → unregisters trailing SL → calls `square_off()`
  - `exit_reason = "scheduled_straddle_squareoff"`

---

## Auto Login (`app/auto_login.py`)

Headless Kite OAuth without a browser (requires `PYOTP_AUTO_LOGIN=true`):
1. POST `kite.zerodha.com/api/login` → `request_id`
2. Generate TOTP from `KITE_TOTP_SECRET` → POST `kite.zerodha.com/api/twofa`
3. GET Connect OAuth endpoint (cookies carry auth session)
4. Follow redirect chain (up to 10 hops) → extract `request_token`
5. `get_session_manager().handle_callback(request_token)` → stores encrypted token
6. Calls `on_success(api_key, access_token)` → caller restarts OrderWatcher WebSocket

Zerodha's stance: unofficial, use at own risk. Guard: `PYOTP_AUTO_LOGIN=false` by default.

---

## ADX (`app/adx.py`)

```python
compute_adx(candles: list[dict], period: int = 14) -> float | None
```
- Pure stdlib, no external deps
- Requires `(2 * period + 1)` candles minimum; returns `None` if insufficient
- Wilder smoothing (alpha = 1/period), same as TradingView's default ADX
- Each candle dict needs: `high`, `low`, `close`
- Used in scheduled straddle to gate NATURALGAS entries

---

## Trade Mode & Options Direction

Controlled by `state.get_trade_mode()` (persisted to `data/state_overrides.json`):

| TRADE_MODE | BUY signal → | SELL signal → |
|------------|-------------|---------------|
| `BUY_OPTIONS` | Buy CE | Buy PE |
| `SELL_OPTIONS` | Sell PE | Sell CE |

- Toggle via `/trade-mode/toggle` POST or `/control` dashboard
- `NO_ENTRY_ON_EXPIRY_DAY=true` blocks new SELL_OPTIONS on weekly expiry day
- Voice commands use explicit side from NLU output (bypasses trade_mode)

---

## ORM Models (`app/storage.py`)

| Model | Key fields | Notes |
|-------|-----------|-------|
| `Alert` | `strategy_id`, `action`, `processed` | Idempotency key; dedup guard |
| `Order` | `kite_order_id`, `status`, `fill_price` | PENDING→COMPLETE/REJECTED/DRY_RUN |
| `Position` | `order_id`, `current_sl`, `quantity` | Mirrors active GTT SL trigger |
| `Gtt` | `kite_gtt_id`, `sl_trigger`, `target_trigger`, `status` | ACTIVE/TRIGGERED/CANCELLED/GTT_FAILED |
| `ClosedTrade` | `pnl`, `exit_reason` | SL_HIT/TARGET_HIT/MANUAL_EXIT/GTT_FILLED/scheduled_straddle_squareoff |
| `Instrument` | `instrument_token`, `tradingsymbol`, `expiry`, `tick_size` | Refreshed daily at 08:30 |
| `StrikeDecision` | `candidates` (JSON), `rejection_reasons` (JSON) | Strike audit trail |
| `KiteSession` | `token_encrypted` | Fernet-encrypted access token |
| `WebSession` | `token` (64-byte hex), `expires_at` | Dashboard session cookie |
| `AppError` | `message`, `traceback` | Caught exceptions |

Relationships: Alert → [Order] → Position → ClosedTrade; Order → Gtt

---

## Key Config Params (`.env` / `app/config.py`)

**Risk**
- `CAPITAL_PER_TRADE=100000` — ₹ budget per trade (premium-based sizing)
- `MAX_DAILY_LOSS_ABS=10000` — ₹ absolute daily loss cap
- `MAX_TRADES_PER_DAY=3`, `MAX_OPEN_POSITIONS=3`, `CONSECUTIVE_LOSSES_LIMIT=3`
- `RR_RATIO=2.0` — target = 2× SL distance

**Options strike selection**
- `TARGET_DELTA=0.65` — BUY_OPTIONS target delta
- `SELL_OPTIONS_TARGET_DELTA=0.50` — ATM for SELL_OPTIONS
- `DELTA_FALLBACK_STEPS=[0.50, 0.35, 0.25]` — tried when capital insufficient
- `SL_PREMIUM_PCT=0.15` — SL if premium drops 15% (BUY mode)

**Straddle (scheduled)**
- `SCHEDULED_STRADDLE_ENABLED=false` — master switch
- `CRUDEOILM_STRADDLE_TIME=22:00`, `CRUDEOILM_STRADDLE_QTY=5`
- `NG_STRADDLE_TIME=22:05`, `NG_STRADDLE_QTY=1`, `NG_STRADDLE_ADX_THRESHOLD=22.0`
- `STRADDLE_SQUAREOFF_TIME=23:20`
- `ADX_PERIOD=14`, `ADX_CANDLE_INTERVAL=10minute`

**Auto login**
- `PYOTP_AUTO_LOGIN=false` — headless login (unofficial)
- `KITE_AUTO_LOGIN_TIME=07:45`
- `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`

**Telegram**
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — notifier is a no-op when empty

**Voice**
- `VOICE_AUTH_TOKEN` — required; 401 if missing
- `VOICE_NLU_MODEL=claude-sonnet-4-6`
- `VOICE_ALLOWED_INSTRUMENTS`, `VOICE_MAX_LOTS=5`, `VOICE_CONFIRM_TTL_SECONDS=60`

---

## Runtime State (`app/state.py`)

All access via getter/setter with `_lock`. Persistent overrides survive restarts via `data/state_overrides.json`.

| Getter | Override key | Notes |
|--------|-------------|-------|
| `is_paper_mode(env_default)` | `_PAPER_MODE` | None = use .env DRY_RUN |
| `is_emergency_stop()` | `_EMERGENCY_STOP` | Blocks all new entries |
| `get_max_lots(env_default)` | `_MAX_LOTS_OVERRIDE` | Per-trade lot cap |
| `get_max_daily_loss(env_default)` | `_MAX_DAILY_LOSS_OVERRIDE` | ₹ loss cap |
| `get_sl_pct(env_default)` | `_SL_PCT_OVERRIDE` | SL% override |
| `get_rr_ratio(env_default)` | `_RR_RATIO` | Target/SL ratio |
| `get_trade_mode()` | persisted | BUY_OPTIONS / SELL_OPTIONS |
| `get_session_invalid()` | in-memory only | Set by daily_session_check |

Orange dot on /control UI = active override present.

---

## Security

- **Webhook**: IP allowlist (`TV_ALLOWED_IPS`) + HMAC constant-time compare
- **Dashboard**: password form → `zb_session` cookie (64-byte, 12h TTL, httponly+secure)
- **Kite token**: PBKDF2(SHA256, SECRET_KEY, salt, 600k) → Fernet → `data/access_token.enc`
- **Voice**: `X-Voice-Auth-Token` header; 30 req/min rate limit
- `SECRET_KEY` must be 32+ chars — used for both session signing and token encryption

---

## Instrument Routing (`app/symbol_mapper.py`)

| Underlying | Exchange | Instrument |
|------------|---------|-----------|
| NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX | NSE → NFO | Options (CE/PE) |
| CRUDEOIL, CRUDEOILM, GOLD, SILVER, etc. | MCX | MCX-OPT options |
| NATURALGAS, NATGASMINI | MCX | Futures (not options) |
| Stocks | NSE | Equity CNC |

MCX tick rounding: all SL/target prices must be rounded via `round_to_tick(price, tick_size)` before GTT placement. Tick sizes: 0.05 (NG) or 0.10 (crude oil options).

---

## MCX Lot Units (PnL multipliers)

```python
MCX_LOT_UNITS = {
    "CRUDEOIL": 100, "CRUDEOILM": 10,
    "NATURALGAS": 1250, "NATGASMINI": 250,
    "GOLD": 100, "GOLDM": 10,
    "SILVER": 30, "SILVERM": 5,
    ...
}
```
Kite stores `lot_size=1` for MCX options in instruments.csv — this map supplies the true contract size for PnL calculations.

---

## Common Debugging Paths

| Symptom | Where to look |
|---------|--------------|
| Alert arrived but no order | `_check_risk_guards()` in main.py; check `data/state_overrides.json` |
| Wrong strike selected | `StrikeDecision` table → `candidates` + `rejection_reasons` JSON |
| GTT not placed | `Gtt` table `status=GTT_FAILED`; check `_on_entry_filled` logs |
| SL not trailing | `TrailingSlManager` logs; 30s throttle may suppress updates |
| Session invalid | Run `daily_session_check()` manually or trigger auto-login |
| MCX price off by tick | `round_to_tick` not called; check `symbol_mapper.py` |
| Straddle skipped | Check ADX gate logs (`[sched_straddle]`); ADX >= threshold |

---

## Tests

```
tests/
  conftest.py         — fixtures (in-memory SQLite, mock kite, settings)
  test_auth.py        — IP allowlist, HMAC, rate limit
  test_config.py      — Settings validation
  test_expiry_resolver.py
  test_greeks.py      — BSM + Black-76 delta/IV
  test_kite_session.py — token encrypt/decrypt
  test_main.py        — webhook end-to-end (mock kite)
  test_orders.py
  test_p1c_signal_routing.py — trade mode routing
  test_risk.py
  test_scheduler.py
  test_schemas.py
  test_storage.py
  test_strike_selector.py
  test_symbol_mapper.py
  test_voice_routes.py
  test_watcher.py
```

Run: `pytest tests/` from `/opt/tv-zerodha-bot`

---

## Deployment

- Docker + docker-compose (local)
- Terraform on GCP
- App: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Reverse proxy: Caddy (`Caddyfile`)
- Data volume: `./data` mounted for DB + token + overrides persistence
