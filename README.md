# tv-zerodha-bot

## What this is

An automated options trading bot that receives buy/sell/exit/trail signals from TradingView alerts, validates them, and routes live orders through Zerodha's Kite Connect API. It selects strikes by delta (BSM for NSE index options, Black-76 for MCX futures), enforces daily-loss and consecutive-loss circuit breakers, and trails GTT OCO stop-losses at bar-close — all without any manual intervention during the trading session.

---

## Architecture

- **TradingView** fires a JSON webhook (Pine Script alert) to `/webhook` on every signal
- **FastAPI** (uvicorn) validates the `AlertPayload` schema and dispatches a background task
- **Signal router** (`_process_alert`) selects the nearest eligible expiry, picks the best-delta strike, sizes the position, and places a Kite Connect MARKET order
- **OrderWatcher** (Kite WebSocket) listens for fill confirmations and places a GTT OCO (SL + target) on fill
- **APScheduler** runs a daily session check at 08:00 IST; blocks live orders until a fresh Kite token is confirmed
- **SQLite** stores alerts, orders, positions, closed trades, and errors (switchable to Postgres for multi-instance)
- **Risk module** enforces per-trade sizing, daily loss cap, consecutive-loss circuit breaker, and open-position limit

---

## Prerequisites

- Python 3.11
- Docker + Docker Compose (for containerised deployment)
- Terraform ≥ 1.6 (for GCP provisioning)
- A GCP account with billing enabled
- A Zerodha Kite Connect API key and secret (from [developers.kite.trade](https://developers.kite.trade))

---

## Local development setup

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/tv-zerodha-bot.git
cd tv-zerodha-bot

# 2. Create .env from template
cp .env.example .env
# Edit .env — at minimum set KITE_API_KEY, KITE_API_SECRET, SECRET_KEY

# 3. Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 4. Run the test suite
pytest

# 5. Start the dev server (DRY_RUN=true keeps all orders offline)
DRY_RUN=true uvicorn app.main:app --reload
# Server starts at http://localhost:8000
```

---

## Environment variables

All variables are read from `.env` (copy from `.env.example`). Key settings:

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `true` | Set `false` only after full validation — suppresses all real orders |
| `TRADING_ENABLED` | `true` | Master kill switch; `false` blocks entries but allows exits |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DATABASE_URL` | `sqlite:///data/bot.db` | SQLite path; switch to `postgresql+psycopg2://...` for multi-instance |
| `WEBHOOK_SECRET` | *(empty)* | Shared secret for verifying TradingView payloads |
| `TV_ALLOWED_IPS` | TradingView IPs | JSON array of IPs allowed to POST `/webhook` |
| `KITE_API_KEY` | *(required)* | From developers.kite.trade → My Apps |
| `KITE_API_SECRET` | *(required)* | From developers.kite.trade → My Apps |
| `KITE_REDIRECT_URL` | *(required)* | e.g. `https://your-ip:8000/kite/callback` |
| `SECRET_KEY` | *(required)* | 32+ char random string; used for access-token encryption |
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Notifier is a no-op when empty |
| `TELEGRAM_CHAT_ID` | *(empty)* | Your personal or group chat ID |
| `CAPITAL_PER_TRADE` | `10000` | ₹ premium budget per option trade |
| `TOTAL_CAPITAL` | `100000` | ₹ total capital; used for % daily-loss cap |
| `MAX_DAILY_LOSS_ABS` | `2000` | ₹ absolute daily loss hard stop |
| `MAX_DAILY_LOSS_PCT` | `2.0` | % of `TOTAL_CAPITAL`; whichever limit hits first wins |
| `MAX_TRADES_PER_DAY` | `10` | Hard cap on entries per day |
| `MAX_OPEN_POSITIONS` | `3` | Maximum concurrent open positions |
| `CONSECUTIVE_LOSSES_LIMIT` | `3` | Circuit breaker; halts trading until manually reset |
| `TARGET_DELTA` | `0.65` | Target option delta for strike selection |
| `OPTION_EXPIRY_RULE` | `NEAREST_WEEKLY` | `NEAREST_WEEKLY` or `NEAREST_MONTHLY` |
| `SL_PREMIUM_PCT` | `0.30` | SL fires if premium drops 30% from entry |
| `BREAKEVEN_RR` | `1.0` | Move SL to breakeven when premium hits entry + 1×risk |
| `TRAIL_RR` | `1.5` | Activate trailing SL when premium hits entry + 1.5×risk |
| `PRODUCT_TYPE` | `NRML` | `NRML` / `MIS` / `CNC` |
| `PBKDF2_ITERATIONS` | `600000` | OWASP 2023 minimum for key derivation |

---

## Running with Docker locally

```bash
# Build and start
docker compose up --build

# Run in background
docker compose up --build -d

# Tail logs
docker compose logs -f

# Stop
docker compose down
```

The container mounts `./data` into `/app/data` for SQLite persistence across restarts.

---

## GCP deployment

### Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- Terraform ≥ 1.6 installed
- A GCP service account with **Compute Admin** and **Service Account User** roles, key saved to `~/.gcp/tv-zerodha-bot-terraform.json`

### Steps

**Step 1 — Review the plan (no changes made):**
```bash
cd terraform
terraform init
terraform plan
```

**Step 2 — Provision infrastructure:**
```bash
terraform apply
```
This creates one `e2-micro` VM in `asia-south1-a` with a reserved static IP, firewall rules for ports 8000 / 22 / 443, and a startup script that installs Docker and clones the repo.

**Step 3 — Register the static IP with Zerodha:**

Note the `external_ip` output. Log in to [developers.kite.trade](https://developers.kite.trade), open your app, and add this IP to the **IP Whitelist** field. Without this, all Kite API calls will be rejected.

**Step 4 — SSH into the VM:**
```bash
gcloud compute ssh tv-zerodha-bot --zone=asia-south1-a --project=tv-zerodha-bot
```
Or use the ready-made `ssh_command` from `terraform output`.

**Step 5 — Configure `.env` and start the bot:**
```bash
cd /opt/tv-zerodha-bot
nano .env          # fill in all REQUIRED fields
docker compose up -d
```

**Step 6 — Verify the bot is running:**
```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

**Step 7 — Set the TradingView webhook URL:**

Use the `webhook_url` output from `terraform output`:
```
http://<external_ip>:8000/webhook
```
Paste this into the **Webhook URL** field when creating TradingView alerts.

---

## Daily Kite login (manual)

Zerodha's Kite Connect access tokens expire at midnight IST every day. The bot enforces this: if the session is invalid, live orders are blocked (dry-run alerts still process).

**Each trading morning before 9:15 IST:**

1. Open `http://<your-ip>:8000/kite/callback` is not the login page — use the Kite login URL your app generates. Typically: `https://kite.zerodha.com/connect/login?api_key=<KITE_API_KEY>`
2. Log in with your Zerodha credentials and TOTP
3. Kite redirects to your `KITE_REDIRECT_URL` (`/kite/callback`) — the bot exchanges the token automatically
4. Check session status: `GET http://<your-ip>:8000/auth/status`
   ```json
   {"session_valid": true, "dry_run": false, "checked_at": "2026-04-27T08:00:00+05:30"}
   ```

The APScheduler job re-validates the token at 08:00 IST daily and sets `SESSION_INVALID=true` if it fails, blocking all live orders until a fresh token arrives.

---

## TradingView alert setup

See [`docs/alert_template.pine`](docs/alert_template.pine) for the canonical Pine Script v5 alert message templates.

**Variants:**

| Action | Purpose |
|---|---|
| `BUY` | Enter long via CE option (or NATURALGAS near-month future) |
| `SELL` | Enter short via PE option |
| `EXIT` | Manually close an open position; cancels GTT and squares off |
| `TRAIL` | Bar-close trail check; requires `premium` field (current option LTP) |

**Webhook URL:** `http://<your-ip>:8000/webhook`

**Alert message format** (BUY example):
```json
{
    "symbol": "{{ticker}}",
    "action": "BUY",
    "price": {{close}},
    "timeframe": "{{interval}}",
    "alert_id": "{{timenow}}",
    "timestamp": "{{timenow}}"
}
```

Unknown fields are rejected with HTTP 422 (`extra="forbid"`). The `alert_id` field is used for idempotency — duplicate firings with the same ID are silently deduplicated.

---

## Tech debt and known limitations

From `app/TECH_DEBT.md`:

- **TD-1: Strike chunking untested above 500 instruments.** The `kite.quote()` call is chunked at 500 strikes, but the merge path has never been exercised. BANKNIFTY at narrow strike intervals could exceed this. Add a fixture with 600+ synthetic instruments before going live with BANKNIFTY.

- **TD-2: Tick rounding unverified in production.** `ROUND_HALF_UP` is used for all price rounding. NSE/MCX may reject orders at certain tick boundaries. Log pre/post-round prices on the first live session and revisit if rejections appear.

- **TD-3: Legacy `SKIP_EXPIRY_DAY_CUTOFF_HOUR/MINUTE` config fields.** These two `int` settings in `config.py` are superseded by `SKIP_EXPIRY_CUTOFF_NSE`/`MCX` string fields added in P1-b but still appear in `Settings`. Remove them in the next cleanup pass to avoid operator confusion.

- **TD-4: `OrderWatcher.start()` not wired to token callback.** The watcher is created at startup but only started when a Kite token arrives via `/kite/callback`. GTT fill events (breakeven/trail) are not tracked until after the first successful daily login.

- **TD-5: Risk gates also block EXIT and TRAIL signals.** `check_risk_gates` sits at the top of `_process_alert` and fires for all action types, including exits. This is intentional per the current spec but should be relaxed for exit paths before production to avoid being locked out of a losing position.

- **TD-6: `GttFilledEvent` catches all COMPLETE SELL orders.** The heuristic fires on any completed SELL order not in the watched-order set, which could match manual broker orders. Add symbol-level matching before going live.
