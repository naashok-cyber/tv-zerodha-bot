# Commodity Agents — Session Handoff (2026-07-06)

Continuation document so any model/session (Sonnet, Opus, or a fresh Fable session)
can pick up exactly where this build stopped. Read alongside `CLAUDE.md` and the
auto-memory file `commodity_agents.md`.

---

## 1. What the system is

Multi-agent commodity/index options decision-support in `app/commodity_agents/`:
deterministic layers (event calendar, regime classifier, strike engine, vol
analytics, Risk Guard with hard veto) + LLM debate (trend / event-gap-risk with
web search / vol agents → judge) → human two-step approve on a PWA dashboard →
optional live execution (short straddle only) through the existing
`_process_straddle` path (GTT OCO + trailing SL).

Underlyings: NATURALGAS, CRUDEOIL, GOLD, SILVER (MCX, Black-76 off front future)
+ NIFTY, BANKNIFTY (NFO, BSM off index spot). `models.exchange_for()` routes.

Safety non-negotiables (do not weaken):
- Regime / Strike / Risk-Guard layers are pure Python, no LLM.
- Risk Guard veto is final; vetoed recs cannot be approved via the API.
- `enforce_gap_risk_gate` downgrades short premium to NO_TRADE in CODE when the
  event agent flags high gap risk.
- All flags default OFF: `COMMODITY_AGENTS_ENABLED`, `COMMODITY_AGENTS_LIVE`.
- Every pipeline stage persists to `AgentRun` (audit trail mandatory).

## 2. Build status — everything below is CODED and UNIT-TESTED locally

### Wave 1 (earlier sessions, all green)
Deterministic core, LLM layer, orchestrator + 3 SQLite audit tables, REST +
dashboard + Analyze PWA pages, Telegram notify, Kite + CSV backtests, gated
execution, NIFTY/BANKNIFTY support. Analyze UI at `/commodity-agents/analyze`
(ticker chips → POST /run → poll `GET /{ticker}/runs/latest` for per-stage
checkmarks → recommendation card with two-step approve).

### Wave 2 — "pro trader" analytics (this session, browser-verified with stubbed pipeline)
- `analytics.py`: VRP (ATM IV − realized vol), IV trend
  (expanding/contracting/stable from stored run history, ≥6 samples), expected
  move vs straddle breakevens (`edge_ratio` = implied/realized;
  `underlying_price` stored for calibration), stress table (±1/2/3% moves ×
  at-expiry and instant IV+5pt shock, INR per lot).
- Stored in new `AgentRun.analytics_json` column (additive ALTER in
  `app/storage.py _migrate`); new "analytics" stage in `routes._STAGES`; fed to
  the LLM debate as a "QUANT ANALYTICS" block in `debate._fmt_context`.
- `GET /{ticker}/iv-history` + SVG IV-vs-RV chart on the Analyze page.
- Real margin check: `orchestrator._margin_required_basket` via
  `kite.basket_order_margins` (SELL legs; NFO qty = lots × lot_size, MCX qty =
  lots; any failure → None → guard skips). `_margin_available` is now
  segment-aware (equity for indices, commodity for MCX).
- 50% profit-take: ALREADY existed in prod (`_on_entry_filled` short-option GTT
  target ≈ 50–52% of premium via `SELL_OPTIONS_PROFIT_BASE/SLOPE/FLOOR`). No change made.
- Bug fixed while verifying: Analyze page trail-loader now targets
  `#trailDet` (old selector grabbed the first `<details>`).

### Wave 3 — remaining recommendations (this session, unit-tested; NOT browser-verified)
- **Phase A**: 0.25Δ bucket in `strikes.TARGET_DELTAS`; `build_chain_snapshot`
  returns (candidates, positioning) — PCR / max pain / call+put OI walls from
  the same quote round (`build_strike_candidates` is now a thin wrapper);
  `next_expiry_atm_iv` → `analytics.term_structure`; `analytics.skew_25d`
  (25Δ risk reversal); India VIX for indices. All rendered in the Analyze
  page's Vol analytics panel (`renderAnalytics` in `dashboard.py`).
- **Phase B**: `models.CORRELATION_GROUPS` (energy / metals / indices) +
  `group_for()`; `risk_guard.RiskInput.open_group_shorts/group_name`
  (defaults keep old callers valid) — ≥1 other underlying in the same group
  with open SELL option positions vetoes new short premium.
  `orchestrator._open_group_shorts` does the Position⋈Order query.
- **Phase C**: `commodity_trade_journal` table (row per APPROVE, mode
  live/paper, entry context frozen: regime, VRP, IV trend, confidence,
  edge ratio); `journal.sync_closed_trades` back-fills P&L via
  Alert→Order→Position→ClosedTrade; `journal.expectancy_stats` by regime;
  `GET /journal`. Calibration: `CommodityRecommendation.realized_move_pct` +
  `outcome` columns (WIN/LOSS for short premium, AVOIDED/MISSED for NO_TRADE,
  UNSCORED when no quant reference); `calibration.evaluate_pending` compares
  daily-normalized realized vs implied move ~20h later; `GET /calibration`
  (judge hit rate, per-agent risk-flag danger rates). Journal write hooks into
  `routes.decide` (APPROVE only); `_maybe_execute` now returns
  `(executed, note, alert_id)`.
- **Phase D**: `portfolio.py` (`compute_portfolio_greeks`: signed delta/vega/
  theta per open option position, straddle grouping by `Order.straddle_id`;
  `check_delta_drift` job → Telegram when |net Δ/lot| ≥
  `COMMODITY_DELTA_ALERT_THRESHOLD` (0.20), 1h throttle; `GET
  /portfolio-greeks`). `briefing.py` 08:45 IST Telegram (overnight gap vs prev
  close from quote ohlc, today's events, VRP/regime per underlying, open
  count). `calendar_refresh.py` Sunday 10:00: event-role LLM with web search
  verifies RBI/FOMC/CPI/OPEC/EIA-shift dates next 45 days → writes STRICTLY
  validated `auto_events` into `data/commodity_events.json` (manual
  `extra_events` preserved; add-only, hardcoded calendar never removed;
  `events._load_override_events` reads both keys). All five jobs registered in
  `orchestrator.register_jobs` (only when `COMMODITY_AGENTS_ENABLED=true`).
- **Phase E**: `iron_fly` added to judge schema + both SHORT_PREMIUM sets;
  judge prompt rule 5 prefers iron fly (sell 0.50Δ, buy 0.25Δ wings, 4 symbols)
  near events/expanding IV; `orchestrator._iron_fly_max_loss` caps est. loss at
  (widest wing width − net credit) × lot_units. Live auto-execution remains
  straddle-only (iron fly → "place manually" note). Backtest: `--cost-pct` /
  `--slippage-pct` → `straddle_pnl_net_pct` + net column in `summarize_pnl`.

### Verified results worth remembering
- BANKNIFTY 436-day CSV backtest with `--cost-pct 1.0 --slippage-pct 0.25`:
  range-bound +1.99% gross → **+0.50% net**; trending +0.83% gross →
  **−0.66% net** (regime gate is what keeps the strategy alive after friction).
- Wave-2 UI browser-verified end-to-end 2026-07-06 via stubbed pipeline
  (scratchpad `ui_demo.py` harness; localStorage token is per-origin — test on
  the same host you set it on).

## 3. Test state

- Targeted suites green: `tests/test_commodity_agents.py` +
  `tests/test_commodity_routes.py` + `tests/test_storage.py` = 109 passed
  (includes ~25 new tests for waves 2–3).
- Full suite: 363 passed + 4 xfail + 2 xpass after Wave 2. After Wave 3 a full
  run was IN FLIGHT when this handoff was written — re-run to confirm:
  `./.venv/Scripts/python.exe -m pytest tests/ -q`
  (ALWAYS the venv python; system python has no pytest.)

## 4. Remaining work / open items (in priority order)

1. **Confirm full suite green** after Wave 3 (see command above). Only
   remaining code task from this session.
2. **Commit to git** — NOTHING from the entire commodity-agents build is
   committed. User must say the word. Suggest logical commits: core system /
   index support / analytics wave / journal+calibration+jobs wave.
3. **Deploy to VM** — must REBUILD the image (`docker compose build bot &&
   docker compose up -d bot`), never just restart (untracked files don't exist
   on the VM — see memory `feedback_deployment.md`). Set
   `COMMODITY_AGENTS_ENABLED=true`, keep `COMMODITY_AGENTS_LIVE=false`.
   First live-data run should happen during market hours with a valid Kite
   session. DB migrations are automatic (additive ALTERs in `init_db`).
4. **User decisions still open** (Section 7 of original brief): paid
   news/calendar API vs LLM web search (calendar_refresh.py is the interim
   answer); monthly LLM budget; paper-validation window length; first live
   underlying (brief suggested GOLD, but note default
   `COMMODITY_MAX_LOSS_PER_LOT=15000` vetoes GOLD straddles ≈ ₹2.2L/lot —
   needs tuning either way).
5. **Verify 2026 event dates** — RBI MPC dates in `events.py` are APPROXIMATE,
   FOMC hardcoded; the Sunday calendar-refresh job will correct forward dates
   once deployed, but a one-time manual check of rbi.org.in is cheap insurance.
6. **Not built (deliberately deferred — candidate next enhancements)**:
   - MAE/MFE per trade (needs tick hooks in `trailing.py`).
   - Strangle/iron-fly auto-execution (only straddle executes live).
   - Walk-forward splits in the backtest (no fitted params yet, so low value).

   (The Desk page at `/commodity-agents/desk` — greeks, journal, scorecard —
   was built and browser-verified 2026-07-07, commit 6683306.)

## 5. Gotchas for the next model

- SQLite returns NAIVE datetimes despite `DateTime(timezone=True)` — normalize
  with `.replace(tzinfo=IST)` before arithmetic (calibration.py does this).
- Kite instrument dump has `lot_size=1` for MCX options; true contract size is
  `settings.MCX_LOT_UNITS`. NFO rows carry real lot_size. Order quantities:
  MCX = lots, NFO = lots × lot_size (margin helper + strikes lot_units both
  encode this).
- `strikes.build_strike_candidates` callers now get 4 delta buckets
  (0.25/0.50/0.65/0.80); anything assuming 3 buckets breaks.
- `routes._maybe_execute` returns a 3-tuple now.
- The Analyze/dashboard pages are Python string constants in `dashboard.py` —
  plain `"""` strings (not f-strings), JS uses string concatenation, escape
  `'` as `\\'` inside onclick attributes.
- Journal rows are written for PAPER approvals too (mode="paper"); only live
  rows get P&L back-filled.
- Windows dev box: use `./.venv/Scripts/python.exe`; long bash heredocs get
  mangled — write files with the Write tool instead.
- Local PC has no Kite session — pipeline runs need the VM or the stubbed
  harness (scratchpad `ui_demo.py`, recreate `.claude/launch.json` pointing at
  it for browser checks; delete after).

## 6. Quick reference

```
# tests (venv python always)
./.venv/Scripts/python.exe -m pytest tests/ -q
./.venv/Scripts/python.exe -m pytest tests/test_commodity_agents.py tests/test_commodity_routes.py -q

# offline backtest with friction
python -m app.commodity_agents.backtest --csv-dir data/banknifty_futures --cost-pct 1.0 --slippage-pct 0.25

# key endpoints (all need X-Admin-Auth-Token)
GET  /commodity-agents/analyze            # UI
GET  /commodity-agents/dashboard          # UI
GET  /commodity-agents/desk               # UI: greeks + journal + scorecard
POST /commodity-agents/run                # {"commodity": "GOLD"} or null=all
GET  /commodity-agents/{t}/runs/latest    # live stage progress + rec + analytics
GET  /commodity-agents/{t}/iv-history     # IV/RV/VRP series
GET  /commodity-agents/journal            # entries + expectancy by regime
GET  /commodity-agents/calibration        # judge hit rate + agent flag reliability
GET  /commodity-agents/portfolio-greeks   # live position greeks
POST /commodity-agents/decision           # two-step approve/reject
```
