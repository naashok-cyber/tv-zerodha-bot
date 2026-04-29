# Development Log — TV-Zerodha Bot

A chronological record of design decisions, bug fixes, and architectural changes.
Use this to understand *why* the code is the way it is.

---

## Phase 1 — Signal Routing & Core Engine

**Commits:** `feat: P2-a scheduler + session gate` and earlier

### What was built
- FastAPI webhook endpoint (`/webhook`) accepting TradingView JSON alerts
- IP allowlist guard (only TradingView IPs accepted)
- Alert deduplication via `alert_id` TTL cache to prevent double-fills
- Signal router: BUY → CE option, SELL → PE option, EXIT → square-off, TRAIL → adjust SL
- `symbol_mapper.py`: maps TV tickers (e.g. `NIFTY`, `CRUDEOIL1!`) to Kite `Instrument` rows
- `risk.py`: lot sizing (`compute_lots`), tick rounding, position sizing

### Key decisions
- **`instrument_type` on `AlertPayload` defaults to `"OPTIONS"`** so existing TV alerts don't need updating.
- **`resolve_continuous`** picks the nearest expiry future for `1!` tickers.
- **`_INDEX_NAMES` raises `NotOrderableError`** — spot indices (NIFTY, BANKNIFTY etc.) can't be directly ordered; the bot must route to options/futures instead.

---

## Phase 2 — Alert Templates & Payload Schema

**Commit:** `feat: P2-b alert templates + payload schema`

### What was built
- Finalised `AlertPayload` Pydantic schema with strict validation:
  - `symbol` 1–20 chars, uppercased
  - `price > 0`
  - `action` in `{BUY, SELL, EXIT, TRAIL}`
  - `premium` required when `action == TRAIL`
  - Naive timestamps accepted and treated as UTC, then converted to IST
- TradingView alert message templates for all action types (see `docs/alert_template.pine`)

### TradingView alert payloads

**Options BUY (buys CE):**
```json
{
  "symbol": "{{ticker}}",
  "action": "BUY",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "alert_id": "{{timenow}}",
  "timestamp": "{{time}}"
}
```

**Options SELL (buys PE):**
```json
{
  "symbol": "{{ticker}}",
  "action": "SELL",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "alert_id": "{{timenow}}",
  "timestamp": "{{time}}"
}
```

**Options EXIT:**
```json
{
  "symbol": "{{ticker}}",
  "action": "EXIT",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "alert_id": "{{timenow}}",
  "timestamp": "{{time}}"
}
```

**Options TRAIL** (chart must be on the *option contract*, not the underlying):
```json
{
  "symbol": "{{ticker}}",
  "action": "TRAIL",
  "price": {{close}},
  "premium": {{close}},
  "timeframe": "{{interval}}",
  "alert_id": "{{timenow}}",
  "timestamp": "{{time}}"
}
```

**Equity BUY (CNC):**
```json
{
  "symbol": "{{ticker}}",
  "action": "BUY",
  "instrument_type": "EQUITY",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "alert_id": "{{timenow}}",
  "timestamp": "{{time}}"
}
```

**Equity EXIT:**
```json
{
  "symbol": "{{ticker}}",
  "action": "EXIT",
  "instrument_type": "EQUITY",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "alert_id": "{{timenow}}",
  "timestamp": "{{time}}"
}
```

---

## Phase 3 — Docker + Terraform GCP Deployment

**Commit:** `feat: P3 Docker + Terraform GCP + README`

### Infrastructure
- GCP project: `tv-zerodha-bot`, zone: `asia-south1-a`
- VM: `e2-micro` (free tier), static external IP: `34.100.226.63`
- Docker Compose: single `bot` service, `./data` volume-mounted for SQLite + encrypted token
- Terraform in `terraform/` provisions the VM, firewall rules, and startup script

### Critical Docker lesson (learned the hard way)
`docker compose build: .` **bakes the code into the image**. Only `./data` is volume-mounted.
- `docker restart` does **NOT** pick up `.env` changes or code changes.
- `docker cp` of files into the container has **no effect** after the next restart.
- **Correct redeploy sequence** (always use this):
  ```bash
  cd /opt/tv-zerodha-bot
  sudo git pull origin master
  sudo docker compose down
  sudo docker compose build
  sudo docker compose up -d
  ```

---

## Post-Deployment Fixes — MCX Routing & Reliability

**Commits:** `fix: route all MCX commodities...`, `fix: webhook reliability + MCX routing...`, `fix: route MCX options correctly...`

### Problems fixed
- **MCX options not routing**: NATURALGAS options were hitting NFO path; fixed by checking `is_natural_gas` flag before segment routing.
- **GTT response normalisation**: Kite returns GTT trigger IDs as strings in some responses; normalised to `int` to avoid DB type mismatch.
- **Instrument loading**: Changed from full dump to exchange-specific downloads (`/instruments/NSE`, `/instruments/NFO`, `/instruments/MCX`) — much faster.
- **MCX commodity set** (`_MCX_COMMODITY_NAMES`): Added explicit set of MCX names so they don't fall through to the NSE equity path when no exchange prefix is given.

---

## Session Fixes — Login Flow, OrderWatcher, Equity Trading

**Commits:** `698cdd1`, `9920eea`, `180764e`, `a637cad`

### 1. `/kite/login` endpoint added (`698cdd1`)

**Problem:** No way to initiate the Kite OAuth flow. Users had to manually construct the login URL.

**Fix:** Added `GET /kite/login` that redirects to the Kite OAuth URL. After Zerodha login, Kite redirects to the configured callback URL with a `request_token`, which `/kite/callback` exchanges for an `access_token`.

**Kite redirect URL** (registered in Kite Developer Console):
```
https://outskirts-phrase-retainer.ngrok-free.dev/kite/callback
```

**ngrok** must run as a systemd service on the VM (not local machine) since Kite requires HTTPS:
```
/etc/systemd/system/ngrok.service
→ forwards outskirts-phrase-retainer.ngrok-free.dev → localhost:8000
```

**Daily routine (before 9:15 IST):**
1. Open `https://outskirts-phrase-retainer.ngrok-free.dev/kite/login`
2. Log in with Kite credentials + TOTP
3. Verify: `http://34.100.226.63:8000/auth/status` → `session_valid: true`

### 2. OrderWatcher restart after token expiry (`698cdd1`)

**Problem:** KiteTicker WebSocket returns 403 after daily token expiry. After 50 reconnect attempts, `on_noreconnect` fires — but `_started` flag stayed `True`, so the next call to `start()` was silently ignored even after a fresh login.

**Fix:**
- Added `on_noreconnect` handler that resets `self._started = False`
- Added `restart(api_key, access_token)` method that closes the old ticker and starts fresh
- `/kite/callback` now calls `_watcher.restart()` instead of `_watcher.start()`

### 3. Non-blocking instrument refresh (`698cdd1`)

**Problem:** `refresh_instruments_job` ran synchronously in the FastAPI lifespan `startup` block, blocking the event loop and causing webhook timeouts for all requests during the ~3-minute download.

**Fix:** Wrapped in `loop.run_in_executor(None, ...)` so the lifespan yields immediately and the refresh runs in a thread pool.

### 4. CNC equity trading support (`9920eea`)

**Problem:** Bot only supported options (NRML product). User wanted to buy stocks with CNC product.

**Design:** BUY only (no short selling on CNC). On fill:
- SL = fill price × 1% (`EQUITY_SL_PCT = 0.01` in config)
- Target = fill price × 2% (1:2 R:R, `RR_RATIO = 2.0`)
- GTT OCO placed on *stock price* (not premium), product = CNC

**Qty sizing:** `qty = floor(CAPITAL_PER_TRADE / LTP)`. Set `CAPITAL_PER_TRADE=25000` in `.env` (needed for SWIGGY lot cost ≈ ₹24,830 at lot_size=1300).

**EXIT for equity:** Reads the stored `order.product` to ensure CNC square-off (not NRML).

### 5. Test fixes (`180764e`, `a637cad`)

- **`test_kite_callback_starts_watcher`**: Updated to assert `watcher.restart()` (not `watcher.start()`)
- **`test_timestamp_naive_raises`**: Renamed to `test_timestamp_naive_treated_as_utc` — naive timestamps are intentionally accepted as UTC and converted to IST; previously the test expected a ValidationError.
- **UNIQUE constraint in `test_symbol_mapper`**: The same fixture CSV was being loaded 3 times (once per exchange) causing duplicate `instrument_token` primary keys. Fixed in `refresh_instruments` by deduplicating into a `dict[instrument_token → row]` before inserting, and calling `session.expunge_all()` after the bulk delete to clear stale identity-map references.

---

## Configuration Reference

| Setting | Default | Notes |
|---|---|---|
| `DRY_RUN` | `true` | Set `false` only after full validation |
| `CAPITAL_PER_TRADE` | `25000` | Min needed for SWIGGY options (lot_size=1300) |
| `EQUITY_SL_PCT` | `0.01` | 1% SL for CNC equity |
| `RR_RATIO` | `2.0` | 1:2 risk:reward → target = 2× SL distance |
| `TV_ALLOWED_IPS` | TradingView CIDRs | Webhook IP allowlist |
| `KITE_API_KEY` | — | From Kite Developer Console |
| `KITE_API_SECRET` | — | From Kite Developer Console |

## Live Endpoints

| Endpoint | Purpose |
|---|---|
| `http://34.100.226.63:8000/health` | Health check |
| `http://34.100.226.63:8000/webhook` | TradingView webhook target |
| `https://outskirts-phrase-retainer.ngrok-free.dev/kite/login` | Start daily Kite OAuth |
| `http://34.100.226.63:8000/auth/status` | Verify session valid |
