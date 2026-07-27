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

### Wave 4 — trade-lifecycle layer (2026-07-07, unit-tested)
- **Suggested lot size**: `orchestrator._suggested_lots` = floor(daily loss
  budget / worst-case per lot), clamped by margin capacity and max lots; 0 is a
  legitimate "don't take it" answer. Stored on `CommodityRecommendation.
  suggested_lots`, shown on Analyze/Dashboard cards, and the two-step approve
  now sends it as the actual lots.
- **Regime-flip alert**: `orchestrator._regime_flip_alert` — when a run
  reclassifies a market to trending/high-vol-expansion (vs the previous run)
  AND open short option legs exist there, Telegram fires immediately.
- **Roll suggestion**: delta-drift alerts now include the concrete ~30Δ strike
  on the untested side with live premium (`portfolio._roll_suggestion`).
- **Slippage + MAE/MFE**: trailing manager tracks per-position price extremes
  (`Position.max_favorable_price/max_adverse_price`, flushed on GTT updates and
  unregister); `journal.sync_closed_trades` back-fills `slippage_pct` (fill vs
  analysis quote, premium-weighted), `mae_pct`, `mfe_pct`; shown on the Desk
  journal table. NOTE: excursions only recorded while trailing is registered.
- **Weekly report**: `weekly_report.py`, Sundays 18:00 IST — week's approvals,
  slippage, MAE, expectancy by regime, judge calibration, IV percentile per
  underlying.
- **Desk margins line**: portfolio-greeks endpoint now returns account margins
  (commodity + equity, net/utilised).
- Migrations: 6 more additive ALTERs in `app/storage.py _migrate` (rec
  suggested_lots; journal slippage/mae/mfe; positions excursion columns).

### Wave 5 — token cost, readability, and the NO_TRADE bias (2026-07-27)

Diagnosed from 234 production recommendation rows: **every outcome was
NO_TRADE**, for three stacked reasons, all now fixed.

1. **Regime gate over-fired on 221 of 234 runs (94%).** `regime.classify_regime`
   compared a 5-bar (2.5 h) window against the 20 bars immediately before it on
   *30-minute* candles. On MCX (09:00–23:30) the US-overlap evening genuinely
   runs 2–3× the quiet Indian morning, so ordinary intraday seasonality cleared
   "ratio ≥ 1.5 + one >3σ candle" nearly every evening — observed x2.05, x2.18,
   x2.34, x3.18, x3.88. Those runs return NO_TRADE at confidence 1.0 without
   calling an LLM at all. Fix: `RV_BASELINE_WINDOW` 20 → **40** (spans more than
   one full session, so the baseline is not itself a time-of-day slice) plus
   **IV confirmation** — a realized-vol spike is only a regime change if the
   options market repriced it (`IV_CONFIRM_PERCENTILE = 80`); with < 10 IV
   samples to rank against, demand `VOL_EXPANSION_RATIO_NO_IV = 3.0` instead.
2. **No debate agent ever emitted `risk_flag: "low"`** — across all 13 debated
   runs the tally was trend {medium 7, high 6}, event {medium 7, high 6}, vol
   {medium 6, high 7}. All three system prompts framed the agent purely as a
   risk-hunter and never defined the grades. Fix: shared `debate._RISK_RUBRIC`
   with explicit definitions, "high" reserved for a *named near-term
   mechanism*, plus per-role rebalancing (a quiet tape is a real finding; a fat
   stable VRP is a genuine edge).
3. **The Judge misapplied its own rules.** Verbatim from a stored output: *"The
   Vol agent's risk_flag is 'high', which hard-blocks any short-premium
   greenlight per desk rule 1"* — rule 1 only ever named the **Event** agent.
   And rule 4 ("prefer NO_TRADE when the debate is split") was an always-open
   escape hatch, since an adversarial debate is always somewhat split. Fix:
   rule 1 now says a Trend/Vol "high" is *evidence to weigh, not a veto*; rule 4
   says NO_TRADE "is NOT the safe default" and that a rich VRP with medium flags
   is a trade sized down, not a pass. `enforce_gap_risk_gate` remains the only
   hard block and is still code-level.

**Token cost** (measured, not estimated). The old `_fmt_context` built one
~3712-char blob and sent it identically to all four roles (~928 tok × 4).
Replaced by a frozen `debate.DebateContext` whose `for_trend/for_event/for_vol/
for_judge` render only the slice that role reasons over — the strike chain, the
single biggest block, now goes to the Judge alone. Plus `_compact_strikes`
(6 abbreviated keys, expiry hoisted to the header), recursive float rounding
(`0.24563175887667` → `0.2456`), and per-role `max_tokens` in
`llm_client.DEFAULT_ROLE_MAX_TOKENS` (700/1200/700/1400, was a flat 3000).
Measured on a realistic 24-strike CRUDEOILM context: **6216 chars total across
all four roles vs 14848 before — a 58% cut**.

New **capacity gate** (`orchestrator._capacity_block`) runs the
strategy-independent risk-guard checks (daily-loss breaker, concurrent-position
cap, zero margin) *before* the debate: if one is already failing, no debate
outcome could be approved, so the four LLM calls are skipped. Deliberately a
subset — blackout and correlated-short-vega only bite short premium and the
Judge may answer with a long option, so those stay downstream.

**Readability + persistence.** `_JUDGE_SCHEMA` gained `headline` (one
plain-English sentence < 110 chars, jargon words explicitly banned) and
`plain_reasons` (2–4 bullets < 120 chars); both persist to new
`CommodityRecommendation.headline` / `plain_reasons_json` columns and lead the
Analyze card, with the dense `reasoning_summary` demoted to a "Desk notes"
collapsible. Risk vetoes and both skip gates write their own plain-language
text (`orchestrator._plain_summary` / `_VETO_PLAIN`) rather than leaving a
headline that describes a trade which is not happening.

The Analyze page previously had **no code path that displayed an existing
run** — `renderResult` was reachable only from `pollRun`, which only started
from `analyze()`, and `pollRun` discarded any run older than the current
trigger. So seeing a past result required paying for a new one. Now `restore(t)`
runs on page load and on every chip tap, `/runs/latest` excludes `IV_SAMPLE`
rows (they fire every 5 min and would always be the newest), and the card shows
an "as of" stamp that turns amber past `_ANALYSIS_TTL_SECONDS` (24 h).

### Wave 6 — Claude Code shim backend (2026-07-27)

Manual analyses no longer have to be billed to the Anthropic API. The VM
already runs Claude Code authenticated with a **subscription OAuth token**
(`~/.claude/.credentials.json`), so `claude -p` there costs nothing marginal.
The bot is in Docker and the CLI plus its credentials are on the host, so the
bridge is a small host-side HTTP service:

- `scripts/claude_shim.py` — stdlib-only `ThreadingHTTPServer`. `POST /run`
  (bearer token, one run at a time, 429 when busy) shells out to `claude -p
  --output-format json` inside a `TemporaryDirectory`, so no repo `CLAUDE.md`
  or project settings leak into the prompt. `GET /healthz` reports the CLI
  version and whether a run is in flight.
- `scripts/claude-shim.service` — systemd unit; **must run as the human user**
  that owns the credentials file (root cannot see it), with
  `EnvironmentFile=/etc/claude-shim.env`.
- `docker-compose.yml` gained `extra_hosts: host.docker.internal:host-gateway`
  — that is how the container reaches the host at all.
- `app/commodity_agents/llm_shim.py::ShimClient` mirrors `LlmClient.run(role,
  system, user) -> dict` exactly, so nothing downstream branches on backend.

**The one thing that is genuinely different: it must be a single call.**
Measured 2026-07-27, a headless `claude -p` invocation carries **~18.9K tokens
of fixed scaffolding** (6.1K cache-create + 12.8K cache-read) even with
`--allowed-tools ""` in an empty directory. Four per-role calls would burn
~75K tokens of pure overhead against ~3.3K for the entire API path. So
`debate.run_full_debate` plays all four seats in one turn and
`debate.split_full_debate` unpacks the reply; `_run_stages` still writes
`trend_json` / `event_debate_json` / `vol_json` / `judge_json` separately, so
the UI, the reasoning trail and `calibration.py` cannot tell the backends
apart. The role briefs (`_TREND_BRIEF`/`_EVENT_BRIEF`/`_VOL_BRIEF`) and
`_JUDGE_RULES` are module constants precisely so both paths send identical
text — a test asserts this.

Cost of that trade-off, stated plainly: one turn means the three agents no
longer reason independently, so their outputs correlate more than over the
API. The prompt pushes back on it (write each seat in full before judging) but
does not eliminate it. If `calibration.py` ever shows shim-run debates
agreeing more than API-run ones, that is why.

Backend selection: `orchestrator.make_backend(settings, manual)` — scheduled
cadence always API, manual `/run` prefers the shim
(`COMMODITY_AGENT_MANUAL_BACKEND=shim`) and silently falls back to the API if
the shim is not configured. `GET /commodity-agents/backend` reports configured
vs reachable vs effective; the Analyze page shows it under the button so the
cost is visible *before* the press.

Worth saying once: driving a subscription from an automated server process is
outside what the subscription is for. Manual-only triggering is the defensible
pattern, and is why the scheduled path was deliberately left on the API.

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
