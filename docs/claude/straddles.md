# Straddles — scheduled entry, defense, entry wings, partial booking

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

**Config**: `SCHEDULED_STRADDLE_ENABLED=false` (master switch), `CRUDEOILM_STRADDLE_TIME=22:00`, `CRUDEOILM_STRADDLE_QTY=5`, `NG_STRADDLE_TIME=22:05`, `NG_STRADDLE_QTY=1`, `NG_STRADDLE_ADX_THRESHOLD=22.0`, `STRADDLE_SQUAREOFF_TIME=23:20`.

## Straddle Defense (`app/straddle_defense.py`)

1-min cron (Mon–Fri 09–23 IST), live-toggleable at /control. Watches short straddles (via `compute_portfolio_greeks` straddle groups) and defends them against the evening IV expansion:

- **Trigger**: drawdown-from-peak ≥ `STRADDLE_DEFENSE_DRAWDOWN_TRIGGER` AND IV rose `STRADDLE_DEFENSE_IV_SAMPLES` consecutive 1-min samples. Hysteresis: max 2 alerts/day/straddle + 20-min re-arm. Peak persisted to `data/straddle_defense_state.json`; IV/MTM series in `iv_snapshots` (30-day retention).
- **Modes** (cyclable at /control, persisted): `ALERT` (Telegram only) → `SEMI_AUTO` (creates a `HedgeAction` PROPOSED row; two-tap Approve on /control, TTL 10 min) → `AUTO` (places unattended; requires env-only `STRADDLE_DEFENSE_AUTO_EXECUTE=true` or degrades to SEMI_AUTO).
- **Hedge** = BUY CE + BUY PE wings `STRADDLE_DEFENSE_WING_STEPS` strike intervals outside the shorts, same expiry/qty (straddle → iron butterfly). ₹/day budget cap `STRADDLE_DEFENSE_MAX_HEDGE_COST`. Wing fills recorded as Order + Position rows (no GTT), so EOD squareoff is a backstop.
- **GTT coordination**: short-leg GTTs are cancelled on Kite and marked `SUSPENDED` while wings are on (restore params in `hedge_actions.suspended_gtts`); re-placed verbatim at unwind. Failed restores alert loudly.
- **Unwind**: scheduled at `STRADDLE_DEFENSE_UNWIND_TIME` (AUTO acts; SEMI_AUTO gets one nudge + /control button), force-unwind for all modes at `STRADDLE_DEFENSE_FORCE_UNWIND_TIME` (before the 23:20 straddle squareoff). Wing exits become `ClosedTrade` rows (`exit_reason=HEDGE_UNWIND`).
- **Tuning**: `scripts/replay_straddle_defense.py` replays recorded `iv_snapshots` through the exact trigger functions across a trigger×samples grid.
- Design rationale: pre-hedge window primary (wings are cheap before the ~18:00 IST IV ramp), reactive drawdown trigger is the backstop; never unwind into a scheduled event (EIA NG report Thu 20:00 IST).

Routes: `/control/straddle-defense/{toggle,mode,hedge,hedge/decision,unwind}` (POST).

**Config**: `STRADDLE_DEFENSE_ENABLED=false`, `STRADDLE_DEFENSE_MODE=ALERT` (both live-overridable at /control), `STRADDLE_DEFENSE_AUTO_EXECUTE=false` (env-only hard gate for AUTO placement), `STRADDLE_DEFENSE_DRAWDOWN_TRIGGER=5000`, `STRADDLE_DEFENSE_IV_SAMPLES=3`, `STRADDLE_DEFENSE_WING_STEPS=2`, `STRADDLE_DEFENSE_MAX_HEDGE_COST=6000`, `STRADDLE_DEFENSE_PREHEDGE_TIME=17:45`, `STRADDLE_DEFENSE_UNWIND_TIME=20:45`, `STRADDLE_DEFENSE_FORCE_UNWIND_TIME=23:10`, `ADX_PERIOD=14`, `ADX_CANDLE_INTERVAL=10minute`.

## Defined-Risk Straddle Entry (`app/entry_wings.py`)

`STRADDLE_ENTRY_WINGS_ENABLED` (env + live /control toggle, default off). When on, every new short straddle in `_process_straddle` buys protective wings **before** the short legs go out, turning it into an iron butterfly from t=0 instead of relying on `straddle_defense` to react after a drawdown:

- **Wings**: long CE + long PE, `STRADDLE_ENTRY_WING_STEPS` (default 3) strike intervals outside the short strikes, same expiry/qty. Reuses `straddle_defense`'s chain-derived strike-interval and wing-selection helpers.
- **Cost cap**: `STRADDLE_ENTRY_WING_MAX_COST_PCT` (default 35%) — wings costing more than this share of the straddle's credit are skipped.
- **`STRADDLE_ENTRY_WINGS_REQUIRED`** (default false): when true, a straddle whose wings can't be sourced or priced within cap is abandoned entirely rather than entered naked.
- **Bookkeeping**: wings are Order + Position rows (no GTT — their loss is bounded by the premium paid) plus a `HedgeAction(trigger="entry", status=ACTIVE)`. That trigger value is what stops `straddle_defense` from proposing a *second* hedge on the same straddle, and what excludes it from the defense's timed unwinds (`_maybe_unwind_scheduled` filters `trigger != "entry"`) — entry wings stay on until the straddle itself is squared off.
- **Failure handling**: a half-filled wing pair is reversed immediately; if the short legs then fail to fill, the wings are reversed too (`reverse_entry_wings`).

## Partial Profit Booking (`app/partial_booking.py`)

`PARTIAL_BOOKING_ENABLED` (env + live /control toggle, default off). `partial_booking_job` (1-min cron, Mon–Fri 09–23 IST) banks part of a winner instead of waiting for the full target:

- **Trigger**: position has travelled `PARTIAL_BOOK_TRIGGER_PCT` (default 60%) of the entry→target distance.
- **Action**: closes `PARTIAL_BOOK_QTY_PCT` (default 50%) of the open quantity via a reducing market order, then resizes the GTT to the remainder — at breakeven when `PARTIAL_BOOK_MOVE_SL_TO_BREAKEVEN=true` (default), so the rest rides risk-free. GTT is resized **before** the reducing order goes out, so the position is never larger than its protection; a failed reducing order restores the GTT to full size.
- **Latch**: `Position.partial_booked_qty` — each position books at most once. The banked amount is stored on the position (`partial_booked_pnl`) and folded into the eventual `ClosedTrade.pnl` by every exit path via `storage.booked_partial_pnl()`.
- Skips a position when the breakeven band wouldn't bracket the current LTP (would be rejected by the broker as "trigger already met").
- Mode-scoped like every other job: paper positions are booked by `paper_pnl`-based math with no broker calls; live positions place a real reducing order via `orders.place_entry` (not `orders.square_off`, whose 30 s per-symbol dedup would swallow a genuine full exit arriving right after).
