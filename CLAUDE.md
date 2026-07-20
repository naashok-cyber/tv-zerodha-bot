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
  paper_trading.py   — Paper-mode fill/exit simulation: 1-min monitor closes paper positions on GTT band breach or EOD
  partial_booking.py — 1-min monitor: books part of a winning position past a target% threshold, re-arms GTT on the remainder at breakeven
  entry_wings.py     — Defined-risk straddles: buys protective wings BEFORE the short legs, records an ACTIVE HedgeAction(trigger="entry")
  auto_login.py      — Headless Kite login: password + TOTP → access_token (PYOTP_AUTO_LOGIN)
  delta_hedge.py     — Multi-underlying delta-hedge dispatcher (NATURALGAS + CRUDEOILM); also hosts NG half-exit, straddle ladder, BNF stop-loss
  scheduled_straddle.py — Automated short straddle jobs for CRUDEOILM + NATURALGAS
  straddle_defense.py — 1-min short-straddle monitor: IV-expansion alerts + wing hedging (ALERT/SEMI_AUTO/AUTO)
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
| POST | `/control/partial-booking/toggle` | Enable/disable partial profit booking |
| POST | `/control/entry-wings/toggle` | Enable/disable defined-risk wings at straddle entry |
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
| `run_delta_hedge_job` | every 1 min at `:05s`, Mon–Fri 09–23 | APScheduler cron |
| `paper_monitor_job` | every 1 min at `:20s`, Mon–Fri 09–23 | APScheduler cron |
| `partial_booking_job` | every 1 min at `:35s`, Mon–Fri 09–23 | APScheduler cron |
| `straddle_defense_job` | every 1 min at `:50s`, Mon–Fri 09–23 | APScheduler cron |
| Scheduled straddle squareoff | `STRADDLE_SQUAREOFF_TIME` (23:20) | APScheduler cron |

`_PreciseTimer`: dedicated daemon thread that busy-waits the final 10 s before target to fire within ~100 ms. Used for straddle entries where exact timing matters.

---

## Straddle Defense (`app/straddle_defense.py`)

1-min cron (Mon–Fri 09–23 IST), live-toggleable at /control. Watches short straddles (via `compute_portfolio_greeks` straddle groups) and defends them against the evening IV expansion:

- **Trigger**: drawdown-from-peak ≥ `STRADDLE_DEFENSE_DRAWDOWN_TRIGGER` AND IV rose `STRADDLE_DEFENSE_IV_SAMPLES` consecutive 1-min samples. Hysteresis: max 2 alerts/day/straddle + 20-min re-arm. Peak persisted to `data/straddle_defense_state.json`; IV/MTM series in `iv_snapshots` (30-day retention).
- **Modes** (cyclable at /control, persisted): `ALERT` (Telegram only) → `SEMI_AUTO` (creates a `HedgeAction` PROPOSED row; two-tap Approve on /control, TTL 10 min) → `AUTO` (places unattended; requires env-only `STRADDLE_DEFENSE_AUTO_EXECUTE=true` or degrades to SEMI_AUTO).
- **Hedge** = BUY CE + BUY PE wings `STRADDLE_DEFENSE_WING_STEPS` strike intervals outside the shorts, same expiry/qty (straddle → iron butterfly). ₹/day budget cap `STRADDLE_DEFENSE_MAX_HEDGE_COST`. Wing fills recorded as Order + Position rows (no GTT), so EOD squareoff is a backstop.
- **GTT coordination**: short-leg GTTs are cancelled on Kite and marked `SUSPENDED` while wings are on (restore params in `hedge_actions.suspended_gtts`); re-placed verbatim at unwind. Failed restores alert loudly.
- **Unwind**: scheduled at `STRADDLE_DEFENSE_UNWIND_TIME` (AUTO acts; SEMI_AUTO gets one nudge + /control button), force-unwind for all modes at `STRADDLE_DEFENSE_FORCE_UNWIND_TIME` (before the 23:20 straddle squareoff). Wing exits become `ClosedTrade` rows (`exit_reason=HEDGE_UNWIND`).
- **Tuning**: `scripts/replay_straddle_defense.py` replays recorded `iv_snapshots` through the exact trigger functions across a trigger×samples grid.

Routes: `/control/straddle-defense/{toggle,mode,hedge,hedge/decision,unwind}` (POST).

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

## Paper Trading (`app/paper_trading.py`)

Paper mode (env `DRY_RUN` or the /control paper toggle) runs the **full real pipeline** — expiry resolution, strike selection, risk sizing, SL/target computation — then simulates the fill at LTP instead of placing a Kite order:

- **Entry**: synthetic `PAPER-`-prefixed order id → `_on_entry_filled` fires with LTP as fill price → Position + Gtt(`status=DRY_RUN`) rows persist, `Order.dry_run=True`. Falls back to a stub Order (qty=0) only when no valid Kite session exists for quotes.
- **Exits**: `paper_monitor_job` (1-min cron, Mon–Fri 09–23 IST) quotes LTP for open paper positions and closes them on GTT band breach (SL_HIT / TARGET_HIT), straddle paired-leg exit, or at the same EOD squareoff times as live (sched straddles 23:20, MCX 23:25, NSE 15:25). Writes `ClosedTrade(dry_run=True, strategy_id=...)`.
- **Isolation rules** (critical):
  - Real squareoff/hedge jobs (`eod_squareoff_job`, scheduled/window straddle squareoffs, straddle defense, delta hedge DB mirrors) filter `Order.dry_run == False` — they must never touch paper positions.
  - Risk guards, today-hero, and P&L snapshot are **mode-scoped** (`ClosedTrade.dry_run == current mode`) — paper losses never consume the live loss budget and vice versa.
  - `compute_portfolio_greeks` is mode-scoped, so straddle defense monitors paper straddles in paper mode and only real ones in live mode.
  - Performance card on /control is live-only; the Strategies scorecard shows paper and live separately (📝 badge); /history tags paper rows 📝 and excludes them from period P&L.
- **Limitations**: fills at LTP (no slippage/spread model), static SL (no trailing — live trailing needs GTT modify calls).

## Partial Profit Booking (`app/partial_booking.py`)

`PARTIAL_BOOKING_ENABLED` (env + live /control toggle, default off). `partial_booking_job` (1-min cron, Mon–Fri 09–23 IST) banks part of a winner instead of waiting for the full target:

- **Trigger**: position has travelled `PARTIAL_BOOK_TRIGGER_PCT` (default 60%) of the entry→target distance.
- **Action**: closes `PARTIAL_BOOK_QTY_PCT` (default 50%) of the open quantity via a reducing market order, then resizes the GTT to the remainder — at breakeven when `PARTIAL_BOOK_MOVE_SL_TO_BREAKEVEN=true` (default), so the rest rides risk-free. GTT is resized **before** the reducing order goes out, so the position is never larger than its protection; a failed reducing order restores the GTT to full size.
- **Latch**: `Position.partial_booked_qty` — each position books at most once. The banked amount is stored on the position (`partial_booked_pnl`) and folded into the eventual `ClosedTrade.pnl` by every exit path via `storage.booked_partial_pnl()`.
- Skips a position when the breakeven band wouldn't bracket the current LTP (would be rejected by the broker as "trigger already met").
- Mode-scoped like every other job here: paper positions are booked by `paper_pnl`-based math with no broker calls; live positions place a real reducing order via `orders.place_entry` (not `orders.square_off`, whose 30 s per-symbol dedup would swallow a genuine full exit arriving right after).

## Defined-Risk Straddle Entry (`app/entry_wings.py`)

`STRADDLE_ENTRY_WINGS_ENABLED` (env + live /control toggle, default off). When on, every new short straddle in `_process_straddle` buys protective wings **before** the short legs go out, turning it into an iron butterfly from t=0 instead of relying on `straddle_defense` to react after a drawdown:

- **Wings**: long CE + long PE, `STRADDLE_ENTRY_WING_STEPS` (default 3) strike intervals outside the short strikes, same expiry/qty. Reuses `straddle_defense`'s chain-derived strike-interval and wing-selection helpers.
- **Cost cap**: `STRADDLE_ENTRY_WING_MAX_COST_PCT` (default 35%) — wings costing more than this share of the straddle's credit are skipped.
- **`STRADDLE_ENTRY_WINGS_REQUIRED`** (default false): when true, a straddle whose wings can't be sourced or priced within cap is abandoned entirely rather than entered naked.
- **Bookkeeping**: wings are Order + Position rows (no GTT — their loss is bounded by the premium paid) plus a `HedgeAction(trigger="entry", status=ACTIVE)`. That trigger value is what stops `straddle_defense` from proposing a *second* hedge on the same straddle, and what excludes it from the defense's timed unwinds (`_maybe_unwind_scheduled` filters `trigger != "entry"`) — entry wings stay on until the straddle itself is squared off.
- **Failure handling**: a half-filled wing pair is reversed immediately; if the short legs then fail to fill, the wings are reversed too (`reverse_entry_wings`).

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
| `Position` | `order_id`, `current_sl`, `quantity` | Mirrors active GTT SL trigger; `quantity` is what remains open — `partial_booked_qty`/`partial_booked_pnl` hold the already-realized slice, folded into `ClosedTrade.pnl` at final exit via `storage.booked_partial_pnl()` |
| `Gtt` | `kite_gtt_id`, `sl_trigger`, `target_trigger`, `status` | ACTIVE/TRIGGERED/CANCELLED/GTT_FAILED |
| `ClosedTrade` | `pnl`, `exit_reason`, `strategy_id`, `dry_run` | SL_HIT/TARGET_HIT/MANUAL_EXIT/GTT_FILLED/scheduled_straddle_squareoff; `dry_run=True` = paper trade (excluded from live analytics/guards) |
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

**Straddle defense**
- `STRADDLE_DEFENSE_ENABLED=false`, `STRADDLE_DEFENSE_MODE=ALERT` — both live-overridable at /control
- `STRADDLE_DEFENSE_AUTO_EXECUTE=false` — env-only hard gate for AUTO placement
- `STRADDLE_DEFENSE_DRAWDOWN_TRIGGER=5000`, `STRADDLE_DEFENSE_IV_SAMPLES=3`
- `STRADDLE_DEFENSE_WING_STEPS=2`, `STRADDLE_DEFENSE_MAX_HEDGE_COST=6000`
- `STRADDLE_DEFENSE_PREHEDGE_TIME=17:45`, `STRADDLE_DEFENSE_UNWIND_TIME=20:45`, `STRADDLE_DEFENSE_FORCE_UNWIND_TIME=23:10`
- `ADX_PERIOD=14`, `ADX_CANDLE_INTERVAL=10minute`

**Defined-risk straddle entry**
- `STRADDLE_ENTRY_WINGS_ENABLED=false` — live-overridable at /control
- `STRADDLE_ENTRY_WING_STEPS=3`, `STRADDLE_ENTRY_WING_MAX_COST_PCT=0.35`
- `STRADDLE_ENTRY_WINGS_REQUIRED=false` — true blocks the straddle entirely if wings aren't available

**Partial profit booking**
- `PARTIAL_BOOKING_ENABLED=false` — live-overridable at /control
- `PARTIAL_BOOK_TRIGGER_PCT=0.60`, `PARTIAL_BOOK_QTY_PCT=0.50`
- `PARTIAL_BOOK_MOVE_SL_TO_BREAKEVEN=true`

**Delta hedge — multi-underlying dispatcher** (`app/delta_hedge.py`)

`run_delta_hedge_job` wakes **every minute** and is a dispatcher only: shared guards → **one** `kite.positions()` → one-shot helpers (half-exit / BNF SL / ladder, gated on the NG toggle) → `_process_underlying` for each *due* spec. Never split into one cron per underlying: APScheduler's `max_instances` only excludes a job from itself, so two jobs would race for the 1 req/sec quote bucket and the same margin pool.

| Spec | interval | phase | Fires at |
|------|----------|-------|----------|
| NATURALGAS | 2 | 1 | `:01 :03 :05 … :59` (odd — avoids `:00`, the most contended slot) |
| CRUDEOILM | 5 | 2 | `:02 :07 :12 … :57` |

- Both due 6×/hour (`:07 :17 :27 :37 :47 :57`). **At most one underlying places orders per tick** (bounds tick duration — `_limit_then_market` blocks ~20s — and stops two hedges competing for margin); the loser sets `carryover` and is force-run the next minute.
- `HedgeSpec` holds only **dimensional** knobs (`band_base/min/max` in contract units, `ref_lots`, cadence, `velocity_samples`, `lot_units`). Dimensionless ratios (exponent, residual, cooldown, escalation, direction multipliers, freeze) stay global in `Settings` — they mean the same thing on any underlying.
- **`_matches_underlying` is mandatory** for leg filtering — `"CRUDEOIL"` is a prefix of `"CRUDEOILM25JUL5900CE"`, so a substring test silently hedges the wrong book. It requires an expiry digit after the name.
- State file `data/ng_hedge_state.json` is keyed per underlying (`{date, naturalgas: {...}, crudeoilm: {...}}`) so NG's cooldown/escalation never gates crude.
- ADX is cached 10 min per underlying (candles are 10-min; recomputing each tick burned the 3 req/sec bucket for an identical answer).

**Rate limits** — Kite allows **1 req/sec** on quote/ltp/ohlc, 3/sec historical, 10/sec orders. Four 1-min crons hit that bucket, so they are offset by `second=` (hedge `:05`, paper `:20`, partial `:35`, defense `:50`) and `app.orders.throttled_quote_call` is the process-wide backstop. **Route new quote/ltp calls through it** — `paper_trading`, `partial_booking` and `straddle_defense` still call `kite.ltp`/`kite.quote` directly and are not yet covered.

**Band formula** (units are per-spec: mmBtu for NG, barrels for CRUDEOILM)

Hedge frequency scales as `gamma² / band²` and gamma scales linearly with lots, so a flat band makes a 3× bigger book trade ~9× as often. The band is therefore computed per tick rather than fixed:

```
band = NG_HEDGE_BAND_BASE
     × (n_lots / NG_HEDGE_BAND_REF_LOTS) ^ NG_HEDGE_BAND_EXPONENT   # 2/3 power (Whalley-Wilmott)
     × adx_regime_mult          # CHOPPY 2.00 / MILD 1.43 / TRENDING 1.00
     × direction_mult           # ADVERSE 0.7 when |δ| growing, FAVOURABLE 1.5 when reverting
     × (1 + NG_HEDGE_ESCALATION_PCT × hedges_today)
     clamped to [NG_HEDGE_BAND_MIN, NG_HEDGE_BAND_MAX]
```

- `NG_HEDGE_BAND_BASE=350`, `NG_HEDGE_BAND_REF_LOTS=10`, `NG_HEDGE_BAND_EXPONENT=0.667` — at ref size this reproduces the old flat 350/500/700 bands exactly
- `NG_HEDGE_RESIDUAL_RATIO=0.4`, `NG_HEDGE_MAX_LOTS_PER_TICK=6` — lots are **solved** so one order lands `|δ|` back inside the band; each fallback path (roll / opposite-BUY / futures) re-solves its own size since delta-per-lot differs
- `NG_HEDGE_VELOCITY_SAMPLES=3` — δ-velocity trend confirm; faster than 10-min ADX. Pre-hedge fires at 0.6× band on one vote (imbalance **or** velocity), 0.5× on both
- `NG_HEDGE_COOLDOWN_MIN=10` (bypass at 2× band), `NG_HEDGE_FREEZE_BEFORE_MIN=20` (bypass at 3× band) — no hedging in the last 20 min before `STRADDLE_SQUAREOFF_TIME`
- `NG_HEDGE_REVERSION_SKIP=false` — opt-in hard skip while `|δ|` is shrinking; mostly subsumed by `NG_HEDGE_FAVOURABLE_MULT`
- `NG_DELTA_HEDGE_THRESHOLD=400` — now only the base when the ADX call fails
- `CRUDEOILM_HEDGE_ENABLED=false`, `CRUDEOILM_HEDGE_BAND_BASE=16.0` **barrels**, `CRUDEOILM_HEDGE_BAND_MIN=11.0`, `MAX=90.0`, `VELOCITY_SAMPLES=2` (2 × 5 min = 10-min trend window)

**The band base is derived from live futures price, not a constant.** `F` is already fetched each tick for the delta model, so this is free:

```
band_units = HEDGE_BAND_NOTIONAL_INR / F × vol_factor        # then ADX × size × dir × esc
```

- `HEDGE_BAND_NOTIONAL_INR=96250` (= NG 350 mmBtu × F 275) — **one number for every underlying**, expressed as ₹ of delta-notional the book may be wrong by
- `vol_factor` is per-spec and dimensionless, so it does **not** go stale as prices move: NG `1.0` (the calibration reference), CRUDEOILM `1.32`
- `*_BAND_BASE` are now **fallbacks only**, used if `F` is zero/garbage (`_notional_band_base` returns source `"LIVE"` or `"STATIC"`; the log line shows which)
- Only the ADX *multiplier* is cached (10 min) — the price-derived base is recomputed every tick so a moving `F` flows through immediately

Why: a constant denominated in mmBtu/barrels silently changes meaning whenever the underlying re-rates. Crude moving 5500 → 7963 made a static `16.0` about **30% too tight in rupee terms** with nothing to flag it — at 5500 the correct band was 23.1 bbl.

**Choosing `vol_factor`** (crude, at `F_ng=275` / `F_crude=7963`):

| Anchor | vol_factor | Crude band | Equalises |
|---|---|---|---|
| Notional parity | 1.00 | 12.1 bbl | ₹ exposure per 1-unit move |
| Volatility parity — `× (3.5%/2.0%)` | 1.75 | 21.2 bbl | daily P&L swing → trade frequency |
| **Configured (midpoint)** | **1.32** | **16.0 bbl** | |

Notional parity alone hedges crude too eagerly for how far it actually moves; vol parity alone takes too much absolute risk. The vol ratio is an estimate — `iv_snapshots` has the data to measure it properly.

`BAND_MIN` has a ceiling as well as a floor: it must stay **≤ `band_base × NG_HEDGE_ADVERSE_MULT`**, or the clamp clips the adverse band short of its intended value (at `MIN=12` the adverse band read 12.0 instead of 11.2 — still tighter than neutral, but only 25% tightening instead of 30%).
- State: `data/ng_hedge_state.json` — resets on date rollover; delete to clear a cooldown

**Granularity rule** — before adding any underlying, check `band ÷ (0.5 × lot_units)`. The band must exceed the delta of one ATM lot, or the smallest possible hedge overshoots and the book ping-pongs forever:

| | δ of 1 ATM lot | band | ratio | |
|---|---|---|---|---|
| CRUDEOILM | 5 bbl | 16 | **3.2** | ✅ lands comfortably inside |
| NATURALGAS | 625 mmBtu | 350–728 | **0.56–1.16** | ⚠️ always overshoots slightly |
| BANKNIFTY | 7.5 units | ~1.5 at parity | **0.20** | ❌ unhedgeable at notional parity |

**BANKNIFTY is deliberately excluded** from this job. Beyond the granularity failure: NFO `positions()` reports quantity in *units* not lots (delta would be 15× off, and `n_lots` would read 15 lots as 225); `MCX_LOT_UNITS.get(name, 1250)` would silently give it 1250; `compute_delta` needs **spot** for NSE, not the futures price this job resolves; and `T` uses `SESSION_CLOSE_MCX` (23:30) against a 15:30 close. If ever wanted, it belongs in its own NSE-hours job.

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
