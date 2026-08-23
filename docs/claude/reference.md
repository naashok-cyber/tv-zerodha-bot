# Reference — routes, jobs, ORM, config, state, security, deployment

## HTTP Routes (`app/main.py`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/webhook` | TradingView alert → background `_process_alert` |
| GET | `/control` | Trade Desk — swipeable 4-tab dashboard (see docs/claude/trade-desk.md) |
| GET | `/dashboard` | Raw TradingView alert log (reached from /control's More tab) |
| GET | `/orders` | Open orders page (Orders/GTTs segmented control) |
| GET | `/positions` | Legacy orphan page — not linked from nav |
| GET | `/history` | Closed trades + 90-day performance/scorecard/activity |
| GET | `/gtts` | GTT OCO status page (Orders/GTTs segmented control) |
| POST | `/trade-mode/toggle` | Toggle BUY_OPTIONS ↔ SELL_OPTIONS |
| POST | `/toggle-paper-mode` | Toggle DRY_RUN override |
| POST | `/toggle-emergency-stop` | Set/clear emergency stop |
| POST | `/toggle-trailing` | Enable/disable trailing SL |
| POST | `/update-risk` | Update capital/lot/loss overrides |
| POST | `/control/partial-booking/toggle` | Enable/disable partial profit booking |
| POST | `/control/entry-wings/toggle` | Enable/disable defined-risk wings at straddle entry |
| POST | `/control/ema-analyzer/toggle` | Enable/disable the 1-min EMA crossover analyzer |
| POST | `/control/ema-analyzer/underlyings` | Toggle one scrip in the analyzer's active set (chip picker) |
| POST | `/control/straddle-defense/{toggle,mode,hedge,hedge/decision,unwind}` | Straddle defense controls |
| POST | `/control/hedge/{slug}` | Retune one hedge engine's rebalance thresholds (`reset=1` reverts to .env). Slugs and fields come from `app/hedge_tunables.py`; applies from the next rebalance |
| POST | `/control/{ng-hedge,ng-fmove-hedge,crude-hedge,crudeoilm-fmove-hedge,silverm-hedge}/toggle` | Start/stop one hedge engine. NG and CRUDEOILM each have a band and an F-move engine; enabling both on one underlying double-hedges it |
| GET | `/kite/login` | Start Kite OAuth flow |
| GET | `/kite/callback` | Exchange request_token → access_token |
| GET | `/healthz` | Health check (returns session status) |
| POST | `/voice/transcribe` | Voice/text → NLU → pending order |
| POST | `/voice/confirm` | Execute pending order |
| POST | `/voice/cancel` | Discard pending order |
| GET | `/voice/pending` | List pending orders |
| POST | `/admin/voice/toggle` | Enable/disable voice channel |

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
| `run_delta_hedge_job` | every 1 min at `:05s`, Mon–Fri 09–23 | APScheduler cron |
| `run_ema_analyzer_job` | every 1 min at `:15s`, Mon–Fri 09–23 | APScheduler cron |
| `paper_monitor_job` | every 1 min at `:20s`, Mon–Fri 09–23 | APScheduler cron |
| `partial_booking_job` | every 1 min at `:35s`, Mon–Fri 09–23 | APScheduler cron |
| `straddle_defense_job` | every 1 min at `:50s`, Mon–Fri 09–23 | APScheduler cron |
| Scheduled straddle squareoff | `STRADDLE_SQUAREOFF_TIME` (23:20) | APScheduler cron |

`_PreciseTimer`: dedicated daemon thread that busy-waits the final 10 s before target to fire within ~100 ms. Used for straddle entries where exact timing matters.

**Kite rate limits** — 1 req/sec on quote/ltp/ohlc, 3/sec historical, 10/sec orders. The 1-min crons are offset by `second=` (see table) to avoid colliding on the quote bucket; `app.orders.throttled_quote_call` is the process-wide backstop. **Route new quote/ltp calls through it** — `paper_trading`, `partial_booking` and `straddle_defense` still call `kite.ltp`/`kite.quote` directly and are not yet covered.

## ORM Models (`app/storage.py`)

| Model | Key fields | Notes |
|-------|-----------|-------|
| `Alert` | `strategy_id`, `action`, `processed` | Idempotency key; dedup guard |
| `Order` | `kite_order_id`, `status`, `fill_price` | PENDING→COMPLETE/REJECTED/DRY_RUN |
| `Position` | `order_id`, `current_sl`, `quantity` | Mirrors active GTT SL trigger; `quantity` is what remains open — `partial_booked_qty`/`partial_booked_pnl` hold the already-realized slice, folded into `ClosedTrade.pnl` at final exit via `storage.booked_partial_pnl()` |
| `Gtt` | `kite_gtt_id`, `sl_trigger`, `target_trigger`, `status` | ACTIVE/TRIGGERED/CANCELLED/GTT_FAILED |
| `ClosedTrade` | `pnl`, `exit_reason`, `strategy_id`, `dry_run` | SL_HIT/TARGET_HIT/MANUAL_EXIT/GTT_FILLED/scheduled_straddle_squareoff; `dry_run=True` = paper trade (excluded from live analytics/guards) |
| `Instrument` | `instrument_token`, `tradingsymbol`, `expiry`, `tick_size` | Refreshed daily at 08:30 |
| `StrikeDecision` | `candidates` (JSON), `rejection_reasons` (JSON) | Strike audit trail |
| `KiteSession` | `token_encrypted` | Fernet-encrypted access token |
| `WebSession` | `token` (64-byte hex), `expires_at` | Dashboard session cookie |
| `AppError` | `message`, `traceback` | Caught exceptions |

Relationships: Alert → [Order] → Position → ClosedTrade; Order → Gtt

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

Straddle/defense/entry-wing/partial-booking params → docs/claude/straddles.md. Delta hedge params → docs/claude/delta-hedge.md. EMA analyzer params → docs/claude/ema-analyzer.md.

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
| `is_ema_analyzer_enabled(env_default)` | `_EMA_ANALYZER_ENABLED_OVERRIDE` | None = use .env EMA_ANALYZER_ENABLED |
| `get_ema_analyzer_underlyings(env_default)` | `_EMA_ANALYZER_UNDERLYINGS_OVERRIDE` | None = use .env; set via /control scrip picker |
| `get_session_invalid()` | in-memory only | Set by daily_session_check |
| `get_hedge_tunable(slug, key, env_default)` | `_HEDGE_TUNABLE_OVERRIDES` | Flat `"<slug>.<key>"` dict, not one global per knob; `set_hedge_tunable(..., None)` clears, `clear_hedge_tunables(slug)` resets one engine. Read through `app/hedge_tunables.effective()` so the registry clamp applies |

Orange dot on /control UI = active override present.

## Trade Mode & Options Direction

Controlled by `state.get_trade_mode()` (persisted to `data/state_overrides.json`):

| TRADE_MODE | BUY signal → | SELL signal → |
|------------|-------------|---------------|
| `BUY_OPTIONS` | Buy CE | Buy PE |
| `SELL_OPTIONS` | Sell PE | Sell CE |

- Toggle via `/trade-mode/toggle` POST or `/control` dashboard
- `NO_ENTRY_ON_EXPIRY_DAY=true` blocks new SELL_OPTIONS on weekly expiry day
- Voice commands use explicit side from NLU output (bypasses trade_mode)

## Auto Login (`app/auto_login.py`)

Headless Kite OAuth without a browser (requires `PYOTP_AUTO_LOGIN=true`):
1. POST `kite.zerodha.com/api/login` → `request_id`
2. Generate TOTP from `KITE_TOTP_SECRET` → POST `kite.zerodha.com/api/twofa`
3. GET Connect OAuth endpoint (cookies carry auth session)
4. Follow redirect chain (up to 10 hops) → extract `request_token`
5. `get_session_manager().handle_callback(request_token)` → stores encrypted token
6. Calls `on_success(api_key, access_token)` → caller restarts OrderWatcher WebSocket

Zerodha's stance: unofficial, use at own risk. Guard: `PYOTP_AUTO_LOGIN=false` by default.

## ADX (`app/adx.py`)

```python
compute_adx(candles: list[dict], period: int = 14) -> float | None
```
- Pure stdlib, no external deps; Wilder smoothing (alpha = 1/period), same as TradingView's default ADX
- Requires `(2 * period + 1)` candles minimum; returns `None` if insufficient
- Each candle dict needs: `high`, `low`, `close`
- Used in scheduled straddle to gate NATURALGAS entries

## Security

- **Webhook**: IP allowlist (`TV_ALLOWED_IPS`) + HMAC constant-time compare
- **Dashboard**: password form → `zb_session` cookie (64-byte, 12h TTL, httponly+secure)
- **Kite token**: PBKDF2(SHA256, SECRET_KEY, salt, 600k) → Fernet → `data/access_token.enc`
- **Voice**: `X-Voice-Auth-Token` header; 30 req/min rate limit
- `SECRET_KEY` must be 32+ chars — used for both session signing and token encryption

## Tests

`tests/` — pytest suite: conftest (in-memory SQLite, mock kite, settings), test_auth, test_config, test_expiry_resolver, test_greeks, test_kite_session, test_main (webhook end-to-end), test_orders, test_p1c_signal_routing (trade mode routing), test_risk, test_scheduler, test_schemas, test_storage, test_strike_selector, test_symbol_mapper, test_voice_routes, test_watcher.

Run: `pytest tests/` from `/opt/tv-zerodha-bot`

## Deployment

- Docker + docker-compose (local); Terraform on GCP; reverse proxy Caddy (`Caddyfile`)
- App: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Data volume: `./data` mounted for DB + token + overrides persistence
- Code changes on the VM need `docker compose build bot && docker compose up -d` — a plain restart runs the old image
