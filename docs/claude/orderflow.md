# Order-flow recorder (`app/orderflow.py`)

`ORDERFLOW_ENABLED` (env + live /control toggle, default off). A **read-only** footprint recorder: it folds the Kite WebSocket tick stream into bid×ask ladder bars and renders them at `/orderflow`. It places no orders and never touches positions — nothing here can move the book.

## Why it lives on the socket (the cost answer)

- **Zero REST quota.** Kite's 1-req/sec cap is a REST-side limit. This rides the streaming channel, so it does not compete with the hedge/EMA/defense crons or `app.orders.throttled_quote_call`. The `second=10` offset on the flush cron is bookkeeping consistency only — the job makes no Kite calls at all.
- **Zero extra connections.** It reuses `OrderWatcher`'s ticker (Kite allows 3 per api_key, 3000 tokens each) via `watcher.subscribe_full()`, which upgrades those tokens to FULL mode — a superset of what trailing SL needs, so trailing keeps working unchanged. Packet size goes 44 B → 184 B per tick per tracked token; at a handful of futures that is noise.
- **The tick callback must stay O(1).** `main.py`'s `_fan_out_ticks` calls `_trailing_manager.on_ticks(...)` **first**, then `_orderflow.on_ticks(...)`, on the WebSocket thread. `on_ticks` only mutates in-memory dicts and wraps `_fold` in try/except — a recorder bug can never kill fill detection or SL trailing. Every DB write happens on the scheduler thread in `flush()`.

## Classification — the quote rule

Neither NSE nor MCX publishes an aggressor flag, so every vendor (and TradingView's own Up/Down Volume, which just scans lower-TF candles) infers it. `classify()` uses the strongest inference the data supports, in order:

1. `ltp >= best_ask` → **buy** (offer lifted); `ltp <= best_bid` → **sell** (bid hit).
2. Inside the spread → midpoint side. Kite streams ~1/sec *snapshots*, so the book has often moved by the time we see the print.
3. No usable book → Lee-Ready tick rule against the previous price.
4. Dead flat mid-spread with no prior print → `split`: the volume is halved, not guessed.

Quantity per tick is `volume_traded - last_volume` (a **negative** delta means day rollover and is ignored, never restated as a baseline); LTP/quote-mode ticks fall back to `last_traded_quantity`. This is volume-per-interval classified against the book, not the raw tape — an honest limit, and the same one every non-exchange-member tool has here.

## Aggregation

- Rows are `tick_size × ORDERFLOW_ROW_TICKS`, bucketed as `floor(ltp / row_size)`; bars are `ORDERFLOW_BAR_SECONDS` wide, bucketed on `exchange_timestamp`.
- `resolve_tokens()` maps each configured underlying to its **front-month FUT** (`Instrument.expiry >= today`, earliest first). It is safe to call every minute: it rebuilds the tracking table but **carries `last_volume`/`last_price`/`last_dir` cursors forward**, so a mid-session re-resolve does not restate a whole bar's volume as a fresh baseline.
- `flush()` writes only bars whose `bucket_ts + bar_seconds <= now`; the in-progress bar stays in memory (`force=True` on EOD/shutdown). Empty bars are skipped.

## Reading side

`load_footprint()` returns bars oldest-first with per-row diagonal imbalances and the bar POC. **Imbalances compare diagonally** (ask at price P vs bid at P−1 for buy; bid at P vs ask at P+1 for sell) against `ORDERFLOW_IMBALANCE_RATIO`, with cells under `ORDERFLOW_IMBALANCE_MIN_PCT` of bar volume filtered out so one-lot prints don't paint the ladder.

**A neighbour row must exist inside the bar's own range.** The top row has no ask above it and the bottom row no bid below it — that's a structural absence, not a zero, and treating it as one would mark an imbalance on *every* bar's extremes and manufacture stacked levels out of nothing. A row that was genuinely skipped *within* the range still counts as zero volume. `stacked_levels()` then reports runs of `ORDERFLOW_STACK_LEN` consecutive imbalanced rows.

## Surfaces

- **`GET /orderflow`** (`?symbol=&bars=`) — the footprint grid, sticky price axis, horizontally scrollable inside `.ofwrap` so the page body never overflows at 390px. Green/red tint = imbalance, amber outline = POC. Below it: session Δ / buy / sell / bar count and the stacked-imbalance table. Reached from the **More** tab tile.
- **`GET /api/orderflow/{underlying}`** — same data as JSON (`underlying`, `enabled`, `bar_seconds`, `session`, `bars[]` with `rows[]`).
- **`POST /control/orderflow/toggle`** and **`POST /control/orderflow/symbols`** — /control Settings → Background Jobs. Toggling on subscribes immediately rather than waiting for the next cron. No Telegram on toggle: the feature is read-only, so a flip is not a trading event.
- Scrip chips are a runtime override (`state.get_orderflow_symbols`, persisted to `state_overrides.json`); the menu is `ORDERFLOW_ALLOWED_SYMBOLS` and anything outside it is silently ignored.

## Jobs

| Job | Schedule | Does |
|---|---|---|
| `orderflow_flush_job` | 1-min cron, `second=10`, Mon–Fri 09–23 IST | re-resolve tokens → subscribe (FULL) → flush completed bars |
| `orderflow_prune_job` | daily 02:20 | delete bars + cells older than `ORDERFLOW_RETENTION_DAYS` |

**Config**: `ORDERFLOW_ENABLED=false` (live-overridable at /control), `ORDERFLOW_SYMBOLS=["NATURALGAS","CRUDEOILM"]`, `ORDERFLOW_ALLOWED_SYMBOLS=[NATURALGAS, CRUDEOILM, GOLDM, SILVERM, NIFTY, BANKNIFTY]`, `ORDERFLOW_BAR_SECONDS=60`, `ORDERFLOW_ROW_TICKS=5`, `ORDERFLOW_RETENTION_DAYS=5`, `ORDERFLOW_IMBALANCE_RATIO=3.0`, `ORDERFLOW_IMBALANCE_MIN_PCT=1.0`, `ORDERFLOW_STACK_LEN=3`.

**Storage**: `orderflow_bars` (unique on `instrument_token`+`bucket_ts`) → `orderflow_cells` (cascade delete). One minute of one scrip is a handful of rows; five days of two scrips stays comfortably inside SQLite.

**Pine counterpart**: `pinescript/orderflow_footprint.pine` + `orderflow_cvd.pine` (see `pinescript/ORDERFLOW.md`) approximate the same read on TradingView using lower-timeframe candles. They're for charting where the Kite feed isn't; this recorder is the one with a real book behind it.
