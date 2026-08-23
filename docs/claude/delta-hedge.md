# Delta Hedge — multi-underlying dispatcher (`app/delta_hedge.py`)

`run_delta_hedge_job` wakes **every minute** and is a dispatcher only: shared guards → **one** `kite.positions()` → one-shot helpers (half-exit / BNF SL / ladder, gated on the NG toggle) → `process_underlying` for each *due* spec. Never split into one cron per underlying: APScheduler's `max_instances` only excludes a job from itself, so two jobs would race for the 1 req/sec quote bucket and the same margin pool.

## Module layout (split Aug 2026)

| File | Holds |
|---|---|
| `app/delta_hedge.py` | the dispatcher, `_build_specs`, and back-compat re-exports |
| `app/hedge_core.py` | the whole band engine — `process_underlying`, `_compute_band`, `_notional_band_base`, `_limit_then_market`, `HedgeSpec`, state helpers |
| `app/ng_delta_hedge.py` | NG's `build_spec()` + the three NG-gated one-shots (half-exit, BNF stop-loss, straddle ladder) |
| `app/crudeoilm_delta_hedge.py` | CRUDEOILM's `build_spec()` |

The *code* is per scrip so thresholds, toggles and state move independently; the *tick* stays shared for the quote-bucket and margin reasons above. `app.delta_hedge` still re-exports `_limit_then_market`, `_compute_band`, `_matches_underlying` and friends — the GOLDM / SILVERM / CRUDEOILM-F-move hedgers and the test suite import them from there.

The BANKNIFTY stop-loss lives in `ng_delta_hedge.py`, not `bnf_delta_hedge.py`: it has always been gated on the NG toggle and ridden the NG tick, and moving it would silently change both.

Log prefixes: dispatcher-level lines (guards, cadence, `positions()` failures) are now `[hedge_dispatch]` — they used to be `[ng_hedge]` back when NG was the only book. Per-spec lines are unchanged (`[naturalgas_hedge]`, `[crudeoilm_hedge]` from `spec.prefix`), as are `[ng_halfexit]`, `[ng_ladder]` and `[bnf_sl]`.

| Spec | interval | phase | Fires at |
|------|----------|-------|----------|
| NATURALGAS | 2 | 1 | `:01 :03 :05 … :59` (odd — avoids `:00`, the most contended slot) |
| CRUDEOILM | 5 | 2 | `:02 :07 :12 … :57` |

- Both due 6×/hour (`:07 :17 :27 :37 :47 :57`). **At most one underlying places orders per tick** (bounds tick duration — `_limit_then_market` blocks ~20s — and stops two hedges competing for margin); the loser sets `carryover` and is force-run the next minute.
- `HedgeSpec` holds the **dimensional** knobs (`band_base/min/max` in contract units, `ref_lots`, cadence, `velocity_samples`, `lot_units`) plus the four live-tunable ones (`band_notional_inr`, `vol_factor`, `cooldown_min`, `max_lots_per_tick`) and its registry `slug`. Dimensionless ratios (exponent, residual, escalation, direction multipliers, freeze) stay global in `Settings` — they mean the same thing on any underlying.
- **`_matches_underlying` is mandatory** for leg filtering — `"CRUDEOIL"` is a prefix of `"CRUDEOILM25JUL5900CE"`, so a substring test silently hedges the wrong book. It requires an expiry digit after the name.
- State file `data/ng_hedge_state.json` is keyed per underlying (`{date, naturalgas: {...}, crudeoilm: {...}}`) so NG's cooldown/escalation never gates crude. Resets on date rollover; delete to clear a cooldown.
- ADX is cached 10 min per underlying (candles are 10-min; recomputing each tick burned the 3 req/sec bucket for an identical answer).

## Band formula

Units are per-spec: mmBtu for NG, barrels for CRUDEOILM.

Hedge frequency scales as `gamma² / band²` and gamma scales linearly with lots, so a flat band makes a 3× bigger book trade ~9× as often. The band is therefore computed per tick rather than fixed:

```
band = NG_HEDGE_BAND_BASE
     × (n_lots / NG_HEDGE_BAND_REF_LOTS) ^ NG_HEDGE_BAND_EXPONENT   # 2/3 power (Whalley-Wilmott)
     × adx_regime_mult          # CHOPPY 2.00 / MILD 1.43 / TRENDING 1.00
     × direction_mult           # ADVERSE 0.7 when |δ| growing, FAVOURABLE 1.5 when reverting
     × (1 + NG_HEDGE_ESCALATION_PCT × hedges_today)
     clamped to [NG_HEDGE_BAND_MIN, NG_HEDGE_BAND_MAX]
```

- `NG_HEDGE_BAND_BASE=350`, `NG_HEDGE_BAND_REF_LOTS=10`, `NG_HEDGE_BAND_EXPONENT=0.667` — at ref size this reproduces the old flat 350/500/700 bands exactly
- `NG_HEDGE_RESIDUAL_RATIO=0.4`, `NG_HEDGE_MAX_LOTS_PER_TICK=6` — lots are **solved** so one order lands `|δ|` back inside the band; each fallback path (roll / opposite-BUY / futures) re-solves its own size since delta-per-lot differs
- `NG_HEDGE_VELOCITY_SAMPLES=3` — δ-velocity trend confirm; faster than 10-min ADX. Pre-hedge fires at 0.6× band on one vote (imbalance **or** velocity), 0.5× on both
- `NG_HEDGE_COOLDOWN_MIN=10` (bypass at 2× band), `NG_HEDGE_FREEZE_BEFORE_MIN=20` (bypass at 3× band) — no hedging in the last 20 min before `STRADDLE_SQUAREOFF_TIME`
- `NG_HEDGE_REVERSION_SKIP=false` — opt-in hard skip while `|δ|` is shrinking; mostly subsumed by `NG_HEDGE_FAVOURABLE_MULT`
- `NG_DELTA_HEDGE_THRESHOLD=400` — now only the base when the ADX call fails
- `CRUDEOILM_HEDGE_ENABLED=false`, `CRUDEOILM_HEDGE_BAND_BASE=16.0` **barrels**, `CRUDEOILM_HEDGE_BAND_MIN=11.0`, `MAX=22.0`, `VELOCITY_SAMPLES=2` (2 × 5 min = 10-min trend window). At `MAX=22` the ceiling clips a book ~3× ref size — intended, but it means crude's band stops scaling with size well before NG's does.

## Notional-derived band base

**The band base is derived from live futures price, not a constant.** `F` is already fetched each tick for the delta model, so this is free:

```
band_units = HEDGE_BAND_NOTIONAL_INR / F × vol_factor        # then ADX × size × dir × esc
```

- `HEDGE_BAND_NOTIONAL_INR=96250` (= NG 350 mmBtu × F 275) — **one number for every underlying**, expressed as ₹ of delta-notional the book may be wrong by
- `vol_factor` is per-spec and dimensionless, so it does **not** go stale as prices move: NG `1.0` (the calibration reference), CRUDEOILM `1.32`
- `*_BAND_BASE` are now **fallbacks only**, used if `F` is zero/garbage (`_notional_band_base` returns source `"LIVE"` or `"STATIC"`; the log line shows which)
- Only the ADX *multiplier* is cached (10 min) — the price-derived base is recomputed every tick so a moving `F` flows through immediately

Why: a constant denominated in mmBtu/barrels silently changes meaning whenever the underlying re-rates. Crude moving 5500 → 7963 made a static `16.0` about **30% too tight in rupee terms** with nothing to flag it — at 5500 the correct band was 23.1 bbl.

**Choosing `vol_factor`** (crude, at `F_ng=275` / `F_crude=7963`):

| Anchor | vol_factor | Crude band | Equalises |
|---|---|---|---|
| Notional parity | 1.00 | 12.1 bbl | ₹ exposure per 1-unit move |
| Volatility parity — `× (3.5%/2.0%)` | 1.75 | 21.2 bbl | daily P&L swing → trade frequency |
| **Configured (midpoint)** | **1.32** | **16.0 bbl** | |

Notional parity alone hedges crude too eagerly for how far it actually moves; vol parity alone takes too much absolute risk. The vol ratio is an estimate — `iv_snapshots` has the data to measure it properly.

`BAND_MIN` has a ceiling as well as a floor: it must stay **≤ `band_base × NG_HEDGE_ADVERSE_MULT`**, or the clamp clips the adverse band short of its intended value (at `MIN=12` the adverse band read 12.0 instead of 11.2 — still tighter than neutral, but only 25% tightening instead of 30%).

## Live rebalance thresholds (`app/hedge_tunables.py`)

Retuning a trigger used to mean editing `.env`, rebuilding the image and restarting. It doesn't any more: every engine re-resolves its thresholds **at the top of each tick**, and `/control` writes them to `data/state_overrides.json`, which sits on the persistent `./data` volume and so survives `docker compose build`.

```
state override (data/state_overrides.json)  →  Settings attr (.env)  →  clamped to the registry's [lo, hi]
```

`ENGINES` in `app/hedge_tunables.py` is the single source: the `/control` cards, the POST route and the reset action are all generated from it. Adding an underlying is one `Engine` entry, not new UI code.

| slug | engine | first two knobs |
|---|---|---|
| `ng` | NATURALGAS δ band | band notional ₹, vol factor |
| `ng_fmove` | `app/ng_fmove_hedge.py` | F-move trigger ₹/mmBtu, min &#124;δ&#124; |
| `crudeoilm_band` | CRUDEOILM δ band | band notional ₹, vol factor |
| `crudeoilm_fmove` | `app/crudeoilm_fmove_hedge.py` | F-move trigger ₹/bbl, min &#124;δ&#124; |
| `banknifty` | `app/bnf_delta_hedge.py` | δ band units, cooldown |
| `goldm` | `app/goldm_delta_hedge.py` | F-move trigger ₹, min &#124;δ&#124; |
| `silverm` | `app/silverm_delta_hedge.py` | F-move trigger ₹/kg, residual kg |

Rules that matter:

- **Resolve once per tick, then thread the result through.** `all_effective(settings, slug)` returns every knob in one pass; SILVERM passes it into `_pick_wing` rather than re-reading. Two resolutions in one tick can straddle an edit and size a hedge against a target the trigger check never saw.
- **Never resolve at scheduler-registration time** — that pins the value for the life of the process and puts the redeploy cycle back.
- **The clamp is the safety rail, not the form.** `data/state_overrides.json` is hand-editable, so `effective()` clamps on read, not just on write. A zero-width band would breach every tick and drain the quote bucket and the margin pool.
- **NG and CRUDEOILM share `HEDGE_BAND_NOTIONAL_INR` as a default but have independent override slots** — retuning one no longer drags the other along.
- `band_notional_inr` floors at **0**, not at a sane minimum: 0 is the documented sentinel for "ignore the formula, use the static `*_BAND_BASE`". Small-but-nonzero values are caught by the band floor anyway.
- Posting a value equal to the `.env` default **clears** the override rather than pinning it, so the orange "overridden" marker keeps meaning something.
- Non-trigger knobs (state paths, enable flags, SILVERM's wing OI/δ filters, BNF's bypass multipliers and `MIN_T_DAYS`) stay `.env`-only.

## Two triggers per underlying — band vs F-move

NG and CRUDEOILM each have **two independent engines**, on two different philosophies:

| | δ band | F-move |
|---|---|---|
| NG | `ng_delta_hedge.py` + `hedge_core.py`, on the dispatcher tick | `ng_fmove_hedge.py`, own cron at `:55` |
| CRUDEOILM | `crudeoilm_delta_hedge.py`, same dispatcher tick | `crudeoilm_fmove_hedge.py`, own cron at `:25` |
| Fires on | `\|net δ\|` escaping a price-derived band | `\|F − anchor\| ≥ trigger` **and** `\|δ\|` growing |
| Anchor | none — it is a level check | re-anchors on every hedge and every skip |
| Quiet tape | can still fire (δ drifts on theta/vega) | cannot — needs price travel |
| Instrument | SELL/roll per the order ladder | SELL ATM |

**They share no state.** Different files, different cooldowns, different daily counters, and neither reads the other's. Running both on the same underlying hedges the same book twice on different clocks. `/control` shows a `DOUBLE-HEDGING` pill and the toggle's Telegram message says so, but nothing in code prevents it — that is deliberate, since the combination is occasionally what you want during a fast trend.

**NG's F-move engine has a granularity floor the crude one does not need.** One NG lot is 1250 mmBtu, so one ATM lot carries ~625 mmBtu of δ, and the lot solver rounds toward zeroing δ. `NG_FMOVE_HEDGE_MIN_DELTA_UNITS` defaults to exactly 625 (one ATM lot) so the engine never fires on a breach a single lot would overshoot and sign-flip. Crude needs no such care — its ratio is 3.2 against NG's 0.56. Lowering NG's floor re-opens the ping-pong.

Trigger sizing, from crude's ₹100/bbl on F=7963 (1.26% of F): notional parity gives NG ₹3.46/mmBtu, volatility parity `× (2.0%/3.5%)` gives ₹1.98. The default ₹3.00 sits near the midpoint — the same way `CRUDEOILM_HEDGE_VOL_FACTOR=1.32` was picked between the same two anchors.

## Granularity rule

Before adding any underlying, check `band ÷ (0.5 × lot_units)`. The band must exceed the delta of one ATM lot, or the smallest possible hedge overshoots and the book ping-pongs forever:

| | δ of 1 ATM lot | band | ratio | |
|---|---|---|---|---|
| CRUDEOILM | 5 bbl | 16 | **3.2** | ✅ lands comfortably inside |
| NATURALGAS | 625 mmBtu | 350–728 | **0.56–1.16** | ⚠️ always overshoots slightly |
| BANKNIFTY | 7.5 units | ~1.5 at parity | **0.20** | ❌ unhedgeable at notional parity |

**BANKNIFTY is deliberately excluded** from this job. Beyond the granularity failure: NFO `positions()` reports quantity in *units* not lots (delta would be 15× off, and `n_lots` would read 15 lots as 225); `MCX_LOT_UNITS.get(name, 1250)` would silently give it 1250; `compute_delta` needs **spot** for NSE, not the futures price this job resolves; and `T` uses `SESSION_CLOSE_MCX` (23:30) against a 15:30 close. If ever wanted, it belongs in its own NSE-hours job.
