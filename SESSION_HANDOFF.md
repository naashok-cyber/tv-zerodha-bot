# Session Handoff — 2026-04-25 (night)

## State: P0-c complete. P0-d not started.

---

## Last Completed Phase: P0-c

**Files added / modified in P0-c (committed as `40fc24b`):**

| File | Status |
|---|---|
| `app/symbol_mapper.py` | new — resolve(), resolve_underlying(), round_to_tick(), list_strikes(), list_expiries(), refresh_instruments() |
| `app/storage.py` | modified — added Instrument SQLAlchemy model (Date expiry, nullable strike) |
| `tests/test_symbol_mapper.py` | new — 17 tests; all 6 resolution cases, Oct/Nov/Dec encoding, MCX continuous, tick rounding, list helpers |
| `tests/fixtures/instruments_sample.csv` | new — 20-row fixture (NIFTY weekly/monthly/FUT, BANKNIFTY, RELIANCE, CRUDEOIL, NATURALGAS, GOLDM) |
| `tests/test_storage.py` | modified — added "instruments" to EXPECTED_TABLES set |

**Test result:** 128 passed, 0 failed, 2.72s (Python 3.11.9, pytest 8.4.2)

---

## All Confirmed Decisions (carry forward)

| Decision | Value | Source |
|---|---|---|
| Python | 3.11 via `py` launcher; venv at `.venv` | RESUME_HERE.md |
| Segments | NFO index options + MCX commodity options | RESUME_HERE.md |
| Product | NRML only | RESUME_HERE.md |
| Capital/trade | ₹10,000 | RESUME_HERE.md |
| Total capital | ₹1,00,000 | RESUME_HERE.md |
| Risk cap | 1% | RESUME_HERE.md |
| MAX_DAILY_LOSS | ₹2,000 ABS **or** 2% of total capital — whichever hits first | session |
| Cloud | AWS Mumbai, Terraform generate-only (no apply) | RESUME_HERE.md |
| Trailing | BAR-CLOSE ONLY via TradingView "TRAIL" webhook | session |
| Breakeven/trail | Applied to OPTION PREMIUM (not underlying price) | session |
| DB | SQLite now; Postgres only when multi-instance needed | RESUME_HERE.md |
| Notifications | Telegram only; no-op if token missing — never crash | session |
| pyotp auto-login | DISABLED | RESUME_HERE.md |
| Underlyings at launch | NIFTY weekly first, then BANKNIFTY | RESUME_HERE.md |
| NATURALGAS signal | Routes to near-month future, not option | RESUME_HERE.md |
| Kite Connect app | Not yet created; stub in .env.example | session |
| Telegram bot | Not yet created; stub in .env.example | session |
| HMAC verify | `hmac.compare_digest` on plaintext secret in JSON payload | P0-b |
| Salt storage | Per-install random 16-byte salt at `data/access_token.salt` | P0-b |
| Rate limiter | In-process token bucket (not slowapi; no ASGI dep yet) | P0-b |
| PBKDF2 iterations | **100,000** (current) — bump to 600,000 queued, not yet applied | P0-b |
| round_to_tick | ROUND_HALF_UP — revisit if NSE rejects half-tick boundaries | P0-c |

---

## PBKDF2 Iterations — Still Queued

Value in `app/config.py` and `.env.example` is still `100_000`.
To apply: change both to `600_000`. Test suite uses `PBKDF2_ITERATIONS=1` — no test changes needed.

---

## Current Commit State

```
git log --oneline:

40fc24b  P0-c: symbol_mapper module with Instrument model and tests
8cda702  P0-b: auth and kite_session modules with tests
4946265  P0-a: config and storage modules with tests
724ea14  Pause point: handoff and resume notes
d5497e6  Initial: spec files and prototype
```

---

## Budget Status

- Session used: ~88%
- Week used: ~47% (week resets **2026-04-28 / Monday**)
- **Resume rule: do not start P0-d until session resets (3:20 am tonight) OR week is fresh Monday.**

---

## Next Phase: P0-d

**Scope: `app/orders.py` + `app/watcher.py`**

---

### P0-d Working Spec (from v1 §6 + v3 §3)

#### orders.py responsibilities

1. **`place_entry(kite, instrument, alert, session) -> str | None`**
   - Builds and places the entry order via `kite.place_order()`.
   - `variety="regular"` always (no bracket orders — discontinued by Zerodha).
   - Order types:
     - `MARKET` → `order_type="MARKET"`, `market_protection=-1` (mandatory from 1 Apr 2026; reject locally if missing from config).
     - `LIMIT` → `order_type="LIMIT"`, `price=tick_rounded_limit`.
     - `SL-M` → `order_type="SL-M"`, `trigger_price=tick_rounded_trigger`, `market_protection=-1`.
     - `SL` → `order_type="SL"`, both `price` and `trigger_price` tick-rounded.
   - All prices rounded via `symbol_mapper.round_to_tick()` using `Decimal`; never `float`.
   - `product=settings.PRODUCT_TYPE` (NRML throughout).
   - Quantity: `floor(CAPITAL_PER_TRADE / ltp / instrument.lot_size) * instrument.lot_size`; if quantity < lot_size, log to errors table and return `None`.
   - Writes an `Order` row to the DB (`dry_run=settings.DRY_RUN`).
   - In `DRY_RUN=True`: skips `kite.place_order()` entirely; `Order.kite_order_id` is `None`.
   - Returns `kite_order_id` (or `None` in DRY_RUN / failure).

2. **`place_gtt_oco(kite, instrument, position, fill_price, session) -> int | None`**
   - Called **only** after entry order status is COMPLETE (from watcher postback).
   - Recomputes SL and target from **actual fill price** (not the original alert price):
     - `sl_dist = fill_price * settings.SL_PREMIUM_PCT`
     - `sl_price = fill_price - sl_dist` (for long/buy options)
     - `target_price = fill_price + settings.RR_RATIO * sl_dist`
     - All three prices tick-rounded.
   - `sl_trigger = sl_price`, `target_trigger = target_price` (triggers == limit prices for options; no slippage buffer needed for now).
   - `last_price`: fetch via `kite.ltp(f"{exchange}:{tradingsymbol}")` — mandatory field that sets trigger direction.
   - GTT OCO payload:
     ```python
     order_dict = [
         {"exchange": ex, "tradingsymbol": ts, "transaction_type": "SELL",
          "quantity": qty, "order_type": "LIMIT",
          "product": product, "price": sl_price},       # leg 1 = stoploss
         {"exchange": ex, "tradingsymbol": ts, "transaction_type": "SELL",
          "quantity": qty, "order_type": "LIMIT",
          "product": product, "price": target_price},   # leg 2 = target
     ]
     kite.place_gtt(
         trigger_type=kite.GTT_TYPE_OCO,
         tradingsymbol=ts, exchange=ex,
         trigger_values=[sl_trigger, target_trigger],
         last_price=ltp,
         orders=order_dict,
     )
     ```
   - Leg ordering matters: SL must be leg 0, target leg 1 (Kite convention).
   - Writes a `Gtt` row; updates the `Position` row with `gtt_id`.
   - In `DRY_RUN=True`: skips `kite.place_gtt()`; `Gtt.kite_gtt_id` is `None`.
   - Returns `kite_gtt_id` (or `None` in DRY_RUN / failure).

3. **`cancel_gtt(kite, gtt_id, session) -> None`**
   - Calls `kite.delete_gtt(gtt_id)`; updates `Gtt.status = "CANCELLED"` in DB.
   - In DRY_RUN: skips the API call; still updates DB.

4. **`square_off(kite, instrument, position, session) -> str | None`**
   - Called on EXIT alert or intraday squareoff job.
   - Cancels any active GTT for the position, then places a MARKET order (opposite side).
   - `market_protection=-1` required.
   - Returns `kite_order_id`.

5. **Exponential backoff wrapper**
   - Retry on: `kiteconnect.exceptions.NetworkException`, HTTP 429 (rate limit).
   - No retry on: `InputException`, `TokenException` — these are permanent; log to errors table and send Telegram alert (no-op if token missing).
   - Config: `BACKOFF_MAX_TRIES=5`, `BACKOFF_INITIAL_WAIT_SECS=1.0` (already in config.py), exponential with jitter.

#### watcher.py responsibilities

1. **`OrderWatcher`** class wrapping `KiteTicker`.
   - `start(kite, session_manager)` — initialises KiteTicker with `api_key`, connects, registers callbacks.
   - `subscribe(instrument_tokens)` / `unsubscribe(instrument_tokens)` — manage the live subscription list.

2. **`on_order_update(ws, data)` callback**
   - Fired by KiteTicker on every order status change (postback via websocket).
   - Key transitions to handle:
     - `status == "COMPLETE"` and order is an **entry order** →
       1. Record `fill_price = data["average_price"]`, `fill_qty = data["filled_quantity"]`.
       2. Update `Order` row in DB.
       3. Call `place_gtt_oco(...)`.
     - `status == "REJECTED"` or `"CANCELLED"` for an entry order →
       1. Update `Order.status` in DB.
       2. Write `AppError` row.
       3. Send Telegram alert.
     - `status == "COMPLETE"` and order is a **GTT leg** (SL or target) →
       1. Write `ClosedTrade` row (fill_price = exit premium, compute PnL).
       2. Update `Position` and `Gtt` rows.
       3. Send Telegram fill notification.

3. **`on_ticks(ws, ticks)` callback** (mark-to-market — stub only for P0-d)
   - Stub that logs received ticks; full MTM logic is P1.
   - Must subscribe to `instrument_token` values for all open positions (query DB on connect).

4. **`on_connect` / `on_reconnect` / `on_close` / `on_error` callbacks**
   - `on_connect`: subscribe to open-position tokens.
   - `on_reconnect`: log reconnect attempt + count.
   - `on_close`: log gracefully; do not raise.
   - `on_error`: log; KiteTicker auto-reconnects.

#### Test requirements for P0-d

- All offline; KiteConnect + KiteTicker fully mocked.
- **orders.py tests (~12):**
  - `place_entry` MARKET: verify `market_protection=-1` present in call kwargs.
  - `place_entry` LIMIT: verify `market_protection` is absent.
  - `place_entry` SL-M: verify `market_protection=-1` present.
  - Price tick-rounding: price passed to `place_order` is exact multiple of tick_size.
  - DRY_RUN: `place_order` not called; `Order` row written with `dry_run=True`, `kite_order_id=None`.
  - Sub-lot quantity → returns None, writes AppError row.
  - GTT OCO payload: SL is leg 0, target is leg 1; `trigger_type == GTT_TYPE_OCO`; both prices tick-rounded; `last_price` fetched via `kite.ltp()`.
  - GTT DRY_RUN: `place_gtt` not called; `Gtt` row written.
  - `cancel_gtt`: `delete_gtt` called with correct id; DB row updated.
  - Backoff on NetworkException: retried up to MAX_TRIES.
  - No retry on InputException: single attempt, error logged.
- **watcher.py tests (~5):**
  - `on_order_update` COMPLETE entry → `place_gtt_oco` called once.
  - `on_order_update` REJECTED entry → AppError written, GTT not placed.
  - `on_order_update` COMPLETE GTT leg → ClosedTrade written.
  - `on_connect` → `subscribe()` called with open-position tokens from DB.
  - `on_ticks` stub → does not raise.

#### Rules (same as P0-a / P0-b / P0-c)
- `_env_file=None` in test settings
- Type hints everywhere
- IST timezone awareness for any time-of-day logic
- No live network in any test
- Stop after pytest is green
- Show test output and any decisions beyond spec
- Do NOT start P0-e until user approves P0-d

---

## What To Do When You Resume

1. Open PowerShell, `cd C:\Users\srias\tv-zerodha-bot`
2. Confirm Python: `py -3.11 --version`
3. Activate venv: `.\.venv\Scripts\Activate.ps1`
4. Confirm tests still pass: `.\.venv\Scripts\pytest -q`
5. Start Claude Code: `claude`
6. Tell Claude: **"Resume from SESSION_HANDOFF.md. Proceed with P0-d."**
   (Apply PBKDF2=600,000 first if you want — it's still queued.)

---

## Proposed Full File Tree (reference — unchanged)

```
tv-zerodha-bot/
├── app/
│   ├── main.py, auth.py, config.py, kite_session.py   ← P0-a/b done
│   ├── symbol_mapper.py                                ← P0-c done
│   ├── orders.py, watcher.py                           ← P0-d next
│   ├── expiry_resolver.py, greeks.py, strike_selector.py  ← P1
│   ├── risk.py                                         ← P0 (remaining)
│   ├── storage.py, notifier.py, scheduler.py           ← P0-a done / P2
│   └── templates/  (dashboard.html, positions.html, history.html)
├── tests/
│   └── fixtures/instruments_sample.csv
├── infra/  (Terraform)
├── pinescript/
├── simulation_mode.py
├── Dockerfile, docker-compose.yml, Caddyfile
├── .env.example, requirements.txt, pyproject.toml
└── .github/workflows/ci.yml
```
