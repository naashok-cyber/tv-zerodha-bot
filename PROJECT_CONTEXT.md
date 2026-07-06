# PROJECT CONTEXT — tv-zerodha-bot

## What It Is
Automated options trading bot. TradingView Pine Script alerts → JSON webhook → FastAPI → Zerodha Kite Connect API. No manual intervention during trading session.

## Tech Stack
- Python 3.11, FastAPI + uvicorn, SQLite (upgradeable to Postgres)
- Zerodha Kite Connect API, APScheduler, Telegram notifications
- Docker + Docker Compose, Terraform → GCP (e2-micro, asia-south1-a)
- Working dir: `C:\Users\srias\tv-zerodha-bot`
- Venv: `.venv` (already created); activate with `.\.venv\Scripts\Activate.ps1`

## Signal Flow
```
TradingView webhook → POST /webhook → _process_alert (background task)
  → resolve symbol → resolve expiry → select delta strike
  → size position → place Kite MARKET order
  → OrderWatcher (WebSocket) → on fill → place GTT OCO (SL + target)
  → on GTT fill → record PnL → ClosedTrade
```

## Signal Types
- BUY → CE option entry (or NATURALGAS near-month FUT)
- SELL → PE option entry
- EXIT → cancel GTT + square off position
- TRAIL → bar-close trail: move SL to breakeven or trail up (premium-based)

## Instruments
- NSE index options: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX (weekly)
- MCX commodity options: CRUDEOIL, GOLD, SILVER etc.
- NATURALGAS/NATGASMINI → near-month FUT (options illiquid)
- NSE Equity CNC: BUY only

## Key Config (app/config.py, loaded from .env)
- DRY_RUN=true (default; set false for live trading)
- CAPITAL_PER_TRADE=30000 (₹)
- TOTAL_CAPITAL=100000 (₹)
- TARGET_DELTA=0.65 (fallback chain: 0.50 → 0.35 → 0.25)
- MAX_DAILY_LOSS_ABS=2000 (₹) or MAX_DAILY_LOSS_PCT=2.0% — whichever first
- MAX_TRADES_PER_DAY=10, MAX_OPEN_POSITIONS=3, CONSECUTIVE_LOSSES_LIMIT=3
- SL_PREMIUM_PCT=0.30 (30% premium drop triggers SL)
- PRODUCT_TYPE=NRML
- OPTION_EXPIRY_RULE=NEAREST_WEEKLY

## App Modules
| File | Role |
|---|---|
| app/main.py | FastAPI app, all endpoints, _process_alert pipeline |
| app/config.py | All settings via pydantic-settings |
| app/strike_selector.py | Delta-based strike selection (chunked kite.quote at 500) |
| app/expiry_resolver.py | Nearest weekly/monthly expiry + cutoff logic |
| app/greeks.py | BSM (NSE) and Black-76 (MCX) delta/IV via py_vollib |
| app/risk.py | Sizing (option/futures/equity qty), daily loss tracking, risk gates |
| app/orders.py | place_entry, place_gtt_oco, modify_gtt, cancel_gtt, square_off |
| app/watcher.py | Kite WebSocket listener for EntryFilledEvent / GttFilledEvent |
| app/kite_session.py | Token encryption (PBKDF2+Fernet), OAuth callback |
| app/scheduler.py | Daily 08:00 IST session check + instrument refresh |
| app/storage.py | SQLAlchemy models: Alert, Order, Position, ClosedTrade, Gtt, AppError |
| app/auth.py | IP allowlist + in-process per-IP rate limiter (token bucket) |
| app/symbol_mapper.py | Normalizes TV tickers → underlying name + exchange segment |
| app/schemas.py | AlertPayload (Pydantic, extra=forbid) |
| app/state.py | Module-level SESSION_INVALID flag |

## API Endpoints
| Endpoint | Purpose |
|---|---|
| POST /webhook | Receives TradingView alerts |
| GET /kite/login | Redirects browser to Zerodha OAuth |
| GET /kite/callback | Handles OAuth token exchange |
| GET /auth/status | Session validity + token age |
| GET /healthz | Liveness + token age |
| GET /health | Simple {"status":"ok"} |
| GET /dashboard | Recent 50 alerts (HTTP Basic auth) |
| GET /positions | Open positions |
| GET /history | Closed trade history |
| GET /status | Bot status + recent errors |

## Build Status (as of 2026-05-11)
- 234 tests passing (pytest -q from project root)
- Phases complete: P0-a through P1-d (21 commits on master)
- Last commit: fix: catch stale token at _process_alert gate instead of crashing
- Next phase: P1-e — notifier.py (Telegram on every failure path) + pipeline hardening

## Known Tech Debt (app/TECH_DEBT.md)
- TD-1: Strike chunking untested above 500 instruments (BANKNIFTY risk)
- TD-3: Legacy SKIP_EXPIRY_DAY_CUTOFF_HOUR/MINUTE config fields still present
- TD-4: OrderWatcher not started until first OAuth login (partially fixed)
- TD-5: Risk gates also block EXIT/TRAIL signals (intentional but risky in prod)
- TD-6: GttFilledEvent catches all COMPLETE SELL orders — could match manual broker orders

## How to Start (local dev)
```powershell
cd C:\Users\srias\tv-zerodha-bot
.\.venv\Scripts\Activate.ps1
# First time only: Copy-Item .env.example .env  then fill SECRET_KEY, KITE_API_KEY, KITE_API_SECRET, KITE_REDIRECT_URL
mkdir data -ErrorAction SilentlyContinue
.\.venv\Scripts\pytest -q                          # expect 234 passed
.\.venv\Scripts\uvicorn app.main:app --reload      # http://localhost:8000
```

## Daily Kite Login (every morning before 9:15 IST)
1. Open browser → http://localhost:8000/kite/login
2. Log in with Zerodha credentials + TOTP
3. Kite redirects to /kite/callback — token saved automatically
4. Verify: GET /auth/status → {"session_valid": true}

## Important Constraints
- Tokens expire at midnight IST — must re-login every trading day
- DRY_RUN=true by default — no real orders until explicitly set false
- PRODUCT_TYPE=NRML only (no MIS auto-squareoff)
- Trail is bar-close only via TradingView TRAIL webhook (never tick-level)
- Breakeven/trail applied to OPTION PREMIUM (not underlying price)
- NATURALGAS always routes to FUT, never options
- Kite API cap: 10 orders/second (above needs SEBI algo registration)
