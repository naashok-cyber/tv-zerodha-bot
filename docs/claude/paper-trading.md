# Paper Trading (`app/paper_trading.py`)

Paper mode (env `DRY_RUN` or the /control paper toggle) runs the **full real pipeline** — expiry resolution, strike selection, risk sizing, SL/target computation — then simulates the fill at LTP instead of placing a Kite order:

- **Entry**: synthetic `PAPER-`-prefixed order id → `_on_entry_filled` fires with LTP as fill price → Position + Gtt(`status=DRY_RUN`) rows persist, `Order.dry_run=True`. Falls back to a stub Order (qty=0) only when no valid Kite session exists for quotes.
- **Exits**: `paper_monitor_job` (1-min cron, Mon–Fri 09–23 IST) quotes LTP for open paper positions and closes them on GTT band breach (SL_HIT / TARGET_HIT), straddle paired-leg exit, or at the same EOD squareoff times as live (sched straddles 23:20, MCX 23:25, NSE 15:25). Writes `ClosedTrade(dry_run=True, strategy_id=...)`.
- **Isolation rules** (critical):
  - Real squareoff/hedge jobs (`eod_squareoff_job`, scheduled/window straddle squareoffs, straddle defense, delta hedge DB mirrors) filter `Order.dry_run == False` — they must never touch paper positions.
  - Risk guards, today-hero, and P&L snapshot are **mode-scoped** (`ClosedTrade.dry_run == current mode`) — paper losses never consume the live loss budget and vice versa.
  - `compute_portfolio_greeks` is mode-scoped, so straddle defense monitors paper straddles in paper mode and only real ones in live mode.
  - Performance card on /history is live-only; the Strategies scorecard (same page) shows paper and live separately (📝 badge); /history tags paper rows 📝 and excludes them from period P&L.
- **Limitations**: fills at LTP (no slippage/spread model), static SL (no trailing — live trailing needs GTT modify calls).
