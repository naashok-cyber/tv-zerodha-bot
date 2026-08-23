# tv-zerodha-bot

Production algo-trading bot: TradingView webhooks + voice commands → Zerodha Kite options/futures orders with delta-based strike selection, GTT OCO exits, trailing SL.

**Stack**: FastAPI + uvicorn · KiteConnect 5.2 (REST + WebSocket) · SQLAlchemy 2.0 + SQLite (`data/bot.db`) · Pydantic v2 · APScheduler 3 + `_PreciseTimer` threads · Anthropic Claude voice NLU · py_vollib/scipy greeks · Fernet token-at-rest.

## Deep-dive docs — read the relevant one BEFORE changing that area

| Working on… | Read first |
|---|---|
| `delta_hedge.py` / `hedge_core.py` / `*_delta_hedge.py` / `hedge_tunables.py` (band formula, dispatcher, live thresholds, granularity rule, why BANKNIFTY is excluded) | `docs/claude/delta-hedge.md` |
| `scheduled_straddle.py`, `straddle_defense.py`, `entry_wings.py`, `partial_booking.py` | `docs/claude/straddles.md` |
| `ema_analyzer.py` | `docs/claude/ema-analyzer.md` |
| `paper_trading.py` / anything touching `dry_run` scoping | `docs/claude/paper-trading.md` |
| `/control` UI, `_shell`, `_SWIPE_JS`, CSS | `docs/claude/trade-desk.md` |
| Routes, scheduled jobs, ORM models, config params, state overrides, security, auto-login, deployment | `docs/claude/reference.md` |

## Directory Map

```
app/
  main.py            — FastAPI app, all HTTP routes, signal processor _process_alert (~3200 lines)
  config.py          — Pydantic BaseSettings from .env; IST/UTC/enums
  state.py           — Runtime overrides (data/state_overrides.json) + session flags
  storage.py         — SQLAlchemy ORM models + init_db()
  orders.py          — place_entry, place_gtt_oco, modify_gtt, cancel_gtt, square_off, throttled_quote_call
  watcher.py         — OrderWatcher: WebSocket fill detection + 7s fallback poll
  trailing.py        — Tick-driven SL trailing, 30s GTT throttle
  scheduler.py       — APScheduler jobs + _PreciseTimer; EOD squareoffs
  strike_selector.py — Delta-based strike selection with liquidity guardrails
  greeks.py          — delta_bsm (NSE) + delta_b76 (MCX); IV solvers
  expiry_resolver.py — Nearest eligible expiry (weekly/monthly, skip-expiry-day)
  risk.py            — Position sizing + daily loss tracking
  kite_session.py    — OAuth token exchange + encrypted persistence
  auth.py            — IP allowlist, HMAC webhook verify, rate limiting
  symbol_mapper.py   — Instrument routing (NSE/NFO/MCX), tick rounding
  schemas.py         — AlertPayload (unified webhook schema)
  adx.py             — Wilder-smoothed ADX (stdlib-only)
  paper_trading.py   — Paper-mode fill/exit simulation
  partial_booking.py — Books part of a winner, re-arms GTT at breakeven
  entry_wings.py     — Defined-risk straddles: wings bought BEFORE short legs
  ema_analyzer.py    — 1-min EMA crossover → long CE/PE with fixed ₹ GTT band
  auto_login.py      — Headless Kite login (password + TOTP)
  delta_hedge.py     — Delta-hedge dispatcher: ONE tick, many books (NG + CRUDEOILM)
  hedge_core.py      — Shared band engine: process_underlying, band formula, order ladder
  ng_delta_hedge.py  — NG spec + NG-gated one-shots (half-exit, BNF SL, straddle ladder)
  crudeoilm_delta_hedge.py — CRUDEOILM band-hedge spec
  hedge_tunables.py  — Registry of live-editable rebalance thresholds (drives /control)
  scheduled_straddle.py — Automated short straddles (CRUDEOILM + NATURALGAS)
  straddle_defense.py — 1-min short-straddle monitor: IV alerts + wing hedging
  webauthn_routes.py — Dashboard login (password → session cookie)
  routes/voice.py, routes/admin_voice.py — voice endpoints
  voice/             — nlu, executor, pending store, config, audit, straddle validation
data/               — bot.db, access_token.enc, state_overrides.json, voice_config.json
tests/              — pytest suite; run `pytest tests/`
docs/claude/        — deep-dive docs (see table above)
terraform/          — GCP infrastructure
```

## Signal Pipeline (`_process_alert` in main.py)

```
POST /webhook → AlertPayload validation → IP + HMAC check → idempotency dedup
  → Alert row → 202 → background _process_alert:
      _check_risk_guards() → route by action:
        EXIT → cancel_gtt → square_off · STRADDLE_SHORT → _process_straddle()
        NATURALGAS → futures MARKET · EQUITY → CNC · BUY/SELL CE/PE → resolve_expiry
          → select_strike (delta) → compute_option_qty → place_entry
  → EntryFilledEvent (WS or 7s poll) → _on_entry_filled → place_gtt_oco + trailing
  → GttFilledEvent → _on_gtt_filled → ClosedTrade + PnL
```

**Risk guards** block entries on: emergency stop · invalid session · daily loss ≥ cap · trades/day ≥ max · open positions ≥ max · consecutive SLs ≥ limit · outside entry window (09:30–15:00 NSE / 09:30–23:00 MCX).

**Trade mode** (`state.get_trade_mode()`): `BUY_OPTIONS` = BUY→buy CE, SELL→buy PE; `SELL_OPTIONS` = BUY→sell PE, SELL→sell CE. Voice and EMA analyzer bypass it (explicit side).

## Hard rules — violating these silently breaks production

1. **MCX tick rounding**: every SL/target price through `round_to_tick(price, tick_size)` before GTT placement (0.05 NG, 0.10 crude options).
2. **MCX contract size**: Kite's instruments.csv says `lot_size=1` for MCX options — real multipliers come from `MCX_LOT_UNITS` in symbol_mapper (CRUDEOILM 10, NATURALGAS 1250, GOLDM 10, SILVERM 5, …).
3. **Routing**: NATURALGAS/NATGASMINI → MCX **futures**, not options — unless the payload carries an explicit `option_type` (EMA analyzer does), which routes to options.
4. **Paper/live isolation**: real squareoff/hedge jobs must filter `Order.dry_run == False`; guards and analytics are mode-scoped on `ClosedTrade.dry_run`. Never let one mode touch the other's rows.
5. **Kite rate limits**: 1 req/sec quotes — the five 1-min crons are offset by `second=` (hedge `:05`, ema `:15`, paper `:20`, partial `:35`, defense `:50`); route new quote/ltp calls through `app.orders.throttled_quote_call`.
6. **Delta hedge is ONE dispatcher job** — the *code* is split per scrip (`ng_delta_hedge.py` / `crudeoilm_delta_hedge.py` over `hedge_core.py`), the *tick* is not; never add a second cron (jobs would race the quote bucket + margin). Leg filtering must use `_matches_underlying` (substring matches the wrong book: `"CRUDEOIL"` prefixes `"CRUDEOILM…"`).
7. **Partial booking** uses `orders.place_entry` for the reducing order, not `square_off` (its 30s per-symbol dedup would swallow a real exit right after); GTT resized **before** the reducing order goes out.
8. **Entry wings** record `HedgeAction(trigger="entry")` — that value excludes them from straddle_defense's proposals and timed unwinds.
9. **`AlertPayload.symbol` caps at 20 chars** — long option tradingsymbols (BANKNIFTY monthly) fail validation when routed as EXIT symbols.
10. **`STRADDLE_DEFENSE_AUTO_EXECUTE` is env-only** — the AUTO mode gate must never become a UI toggle.
11. **/control UI**: tab bar markup renders **before** `.wrap` content in `_shell()` (inline JS can't see later DOM); `_status_strip` renders on every page.
12. **Runtime overrides** live in `data/state_overrides.json` via `app/state.py` getters — check there first when behavior contradicts `.env`. Orange dot on /control = override active.
13. **Hedge thresholds resolve per tick** via `app/hedge_tunables.py` (`/control` override → `.env` → clamped on read). Resolve once per tick with `all_effective()` and thread it through — never at scheduler-registration time, never twice in one decision.

## Debugging quick map

| Symptom | Look at |
|---------|---------|
| Alert but no order | `_check_risk_guards()`; `data/state_overrides.json` |
| Wrong strike | `StrikeDecision` table → `candidates`/`rejection_reasons` JSON |
| GTT not placed | `Gtt.status=GTT_FAILED`; `_on_entry_filled` logs |
| SL not trailing | TrailingSlManager logs; 30s throttle |
| Session invalid | `daily_session_check()` / auto-login |
| MCX price off by tick | missing `round_to_tick` |
| Straddle skipped | `[sched_straddle]` ADX gate logs |

## Deploy

Docker compose on GCP VM behind Caddy. Code changes need `docker compose build bot && docker compose up -d` — a restart alone runs the old image. `./data` volume persists DB + token + overrides.
