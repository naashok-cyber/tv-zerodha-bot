# Simplifying `/control` — analysis and plan

Written 2026-09-05, after `/control` grew from a two-column status page to a
4-tab dashboard carrying 9 live feature toggles, 7 hedge-tunable drawers, 14
risk fields, a voice trade console and a 6-tile volatility grid.

The complaint is real and it is measurable. This document separates it into the
three different problems it actually is, because they have three different
fixes and only one of them is "delete a feature".

---

## 1. What the numbers say

`app/main.py` is 6,261 lines. Everything from line ~2300 down is HTTP and HTML:

| Block | Lines | What |
|---|---|---|
| `_CSS` … `_SWIPE_JS` | 2301–3801 | 1,500 lines of CSS + 4 inline JS blobs + the Quick Trade markup |
| `_status_strip`, `_shell`, feed/scorecard helpers | 3801–4189 | shared chrome |
| `/dashboard` `/positions` `/history` `/orderflow` `/gtts` | 4189–4838 | secondary pages |
| `control_page` | 4838–5469 | **631 lines, one function** |
| 20 `/control/*` toggle routes | 5469–6147 | 678 lines, mostly identical |

**~63% of main.py is the UI.** The signal pipeline — the part with the hard
rules in CLAUDE.md — is the first third, and it is the part that gets scrolled
past. That is the "cumbersome" feeling, and it is felt in the editor before it
is ever felt on the phone.

The Settings tab alone renders, in one 177-line f-string: 13 cards, 16 toggle
rows, 18 `<form>`s, 19 pills and 14 number inputs. The status strip adds 9 more
pills on top, on **every page**, of which 6 typically read `… OFF`.

---

## 2. Three separate problems

### P1 — Boilerplate per toggle (a code problem, invisible to the user)

Adding one feature toggle today costs, spread across three files:

- **`app/state.py`**: `is_x_enabled` / `set_x_enabled` / `toggle_x_enabled` — ~35 lines
- **`app/main.py` route**: auth → toggle → log → Telegram → 302, ~20 lines, near-identical to the eight before it
- **`control_page` locals**: `x_enabled`, `x_pill`, `x_label`, `x_lbl`, `x_cls` — 5–7 lines
- **`control_page` markup**: one hand-written `mdr` row — ~8 lines
- **`_status_strip`**: one `spill()` line

That is ~70 lines × 9 toggles ≈ **630 lines of pure repetition**, and it is why
the page feels heavy to change even when it looks fine.

The fix already exists in this codebase and is proven: `app/hedge_tunables.py`
declares an `ENGINES` registry, and `/control/hedge/{slug}` plus
`_hedge_tunable_drawers()` generate the route, the form, the clamp and the reset
for all 7 engines from it. Nothing about that pattern is specific to numbers.

### P2 — Everything is on screen at once (a UI problem)

The page shows every feature at full weight regardless of whether it is running.
A stopped engine occupies the same row height, the same pill and the same
Start/Stop button as a running one, and an `OFF` pill in the status strip
carries no information anyone acts on. Six of them in a row is noise that makes
the three pills that matter (paper/live, Kite session, trade mode) harder to
find — which is the opposite of what an annunciator strip is for.

### P3 — Genuinely dormant features (a product problem)

This is the one you named, and it is the one that must be **measured, not
guessed**. See §3.

---

## 3. Measure before deleting: `scripts/audit_feature_usage.py`

```
docker compose exec bot python scripts/audit_feature_usage.py --days 120
```

Every feature leaves an artefact row behind — an `Alert` with a known
`strategy_id` prefix, a `HedgeAction` with a known `trigger`, an `IvSnapshot`
heartbeat, an `OrderFlowBar`, an `Order` on the underlying. The script walks all
of them and prints, per feature: current toggle state, last activity, its age,
and the event count in the window.

Two rows are worth acting on:

- `off` + `never` → **nothing has ever used this**. Deletion candidate.
- `ON` + 0 events → **worse**: enabled, silent, and still burning a cron tick
  and a slot in the 1 req/sec quote bucket every minute.

Run it before touching §5. The list below is what the *code* says is dormant;
only the DB knows what you actually stopped using.

---

## 4. The structural fix — one feature registry

Mirror `hedge_tunables.ENGINES` with a `Feature` registry (`app/features.py`):

```python
@dataclass(frozen=True)
class Feature:
    slug: str              # "ema_analyzer" — namespaces the override key AND the POST path
    title: str             # "EMA Analyzer"
    group: str             # "engines" | "risk" | "mode"
    env_attr: str          # "EMA_ANALYZER_ENABLED"
    subtitle: Callable[[Settings], str]   # the live-values line under the label
    on_label: str = "ON"
    off_label: str = "OFF"
    telegram: str | None = None           # message template on toggle
    conflicts_with: tuple[str, ...] = ()  # drives the DOUBLE-HEDGING pill
```

Then delete, per feature:

- the `set_` / `toggle_` pair in `state.py` → one generic
  `state.is_feature_on(slug, default)` / `set_feature(slug, v)` /
  `toggle_feature(slug, default)` trio reading the same
  `data/state_overrides.json`. **Keep the existing override key names** — a
  rename orphans live overrides on the VM, exactly as the `hedge_tunables`
  docstring warns about slugs.
- the bespoke route → one `POST /control/feature/{slug}`, with the Telegram line
  and the conflict warning coming from the registry
- the 5–7 pill locals and the 8-line markup row → one `_feature_row(f, settings)`
- the `_status_strip` line → a loop over `[f for f in FEATURES if is_on(f)]`

**Keep the named `is_*_enabled` getters as one-line aliases over the generic
pair.** They are not UI code: every engine calls its own in its hot path
(`delta_hedge.py:89`, `ema_analyzer.py:303`, `partial_booking.py:250`,
`straddle_defense.py:833`, `window_straddle.py:126`, the three F-move modules),
and `tests/` calls the named getters and setters directly — 40-odd call sites
across six test files. Aliasing keeps all of that untouched and confines the
refactor to the UI, which is the point. Collapse the aliases later, or never.

**Expected: ~500 lines out of `state.py` + `main.py`, and adding the next
feature becomes one `Feature(...)` entry instead of edits in three files.**

Two things must survive the refactor unchanged, because they are load-bearing:

- `STRADDLE_DEFENSE_AUTO_EXECUTE` stays env-only (CLAUDE.md rule 10) — the
  registry must not be able to express it.
- The `DOUBLE-HEDGING` pill and its Telegram warning (rule 14) become the
  `conflicts_with` field rather than hand-written `if` branches — same output,
  declared once.

Order matters: **do this before deleting anything.** A registry makes the
deletions in §5 one-line removals instead of five-file surgery.

---

## 5. What to retire (verify each against the audit first)

**Certain — dead code, no judgement call needed:**

- `GET /positions` (main.py:4229) — a bare 4-column table of `Position` rows,
  linked from nowhere, superseded by Home's live-greeks card. It even passes
  `_shell("")` — a tab key that matches no tab.
- `GET /status` (main.py:4652) — a 302 to `/control`, described in its own
  docstring as legacy. Keep only if an old bookmark still points at it.

**Engines with no UI at all — decide, don't leave in limbo:**

- `goldm_delta_hedge.py` (396 lines) and `bnf_delta_hedge.py` (504 lines) have
  cron registrations and `hedge_tunables` entries but **no `/control` toggle**.
  They are `.env`-only, which means the only way to notice one is running is to
  read the container env. Once §4 lands, wiring them in is one registry line
  each — do that, or delete both. The current half-state is the worst of the
  three options.

**Audit-gated candidates, cheapest to remove first:**

| Feature | Cost if removed | Notes |
|---|---|---|
| Order Flow recorder + `/orderflow` | `orderflow.py` 596 + page ~200 + `_ORDERFLOW_CSS` + 2 tables | newest feature (2026-08-23); read-only, costs no Kite quota, so cheap to *keep* — but also the least proven |
| Window straddle | `window_straddle.py` 393 + rail entries | default off |
| Scheduled straddle | `scheduled_straddle.py` 336 | default off; note `straddle_defense` and `paper_trading` both reference its `strategy_id` |
| Quick Trade / voice | 338 lines of markup + `_QUICK_TRADE_PANEL` JS + 7 proxy routes + `app/voice/` | biggest single block on the tab you open most |
| Three agent pages | `/dashboard`, `/commodity-agents/dashboard`, `/commodity-agents/desk` | three "look at the agents" destinations in a 7-tile More menu; likely one is enough |

Note the asymmetry: removing an *engine* removes a cron tick and quote-bucket
pressure; removing a *page* only removes lines. If the audit says an engine is
`ON` but silent, turning it off is a bigger win than deleting any page.

---

## 6. The UI changes (independent of §4 and §5 — ship in any order)

1. **Status strip: show what is ON.** Keep paper/live, Kite, trade mode and the
   override count always; render feature pills only when enabled. A strip that
   is 3 pills on a quiet day and 6 on a busy one is scannable; nine pills, six
   of them `OFF`, is not.

2. **Fold stopped engines.** Background Jobs becomes: running engines as rows,
   stopped ones collapsed into `<details>` — "4 stopped engines". Same controls,
   one tap away, and the card stops being 7 rows of mostly-`STOPPED`.

3. **Pair each engine with its own tunables.** The Rebalance Triggers drawers
   are rendered as a separate stack below Background Jobs, so tuning NG means
   scrolling past four other engines. With §4's registry both come from the same
   `Feature`, so the drawer can nest inside the engine's row — the toggle says
   whether it runs, the drawer says what makes it fire, and they finally sit
   together.

4. **Collapse Quick Trade behind a button** if the audit shows light use. It is
   the single largest block on Home and the one thing there that is not a
   number you glance at.

5. **Settings tab is doing two jobs.** Split it: *Settings* keeps Mode, Kite
   session and Risk Parameters (things you set); a new *Engines* tab takes
   Background Jobs, Rebalance Triggers and Risk Controls (things that run).
   The tab bar has room — More is 7 link-tiles and ~280px tall.

---

## 7. Extract the UI out of `main.py`

Pure move, no behaviour change, and it is what makes the file navigable again:

```
app/ui/
  css.py        _CSS, _ORDERFLOW_CSS
  shell.py      _shell, _status_strip, _book_seg
  scripts.py    _CONTROL_LIVE_JS, _HISTORY_LIVE_JS, _VOL_GRID_JS, _SWIPE_JS
  quicktrade.py _QUICK_TRADE_PANEL
  control.py    control_page + the /control/* routes (an APIRouter)
  pages.py      /orders /gtts /history /dashboard /orderflow
```

`main.py` drops to ~2,300 lines and becomes what CLAUDE.md's directory map
already claims it is: the app, the routes, and `_process_alert`.

One constraint carries over verbatim (CLAUDE.md rule 11): the tab bar markup
must still render **before** `.wrap`'s content in `_shell()`, or `_SWIPE_JS`
captures an empty node list and tab highlighting silently no-ops.

---

## 8. Suggested order

1. Run `scripts/audit_feature_usage.py --days 120`. **Nothing else starts until
   this has run against the live DB.**
2. Delete `/positions` and `/status` (§5, certain).
3. Build the `Feature` registry and migrate the 9 toggles (§4). Keep override
   keys byte-identical and keep the named getters as aliases; `tests/` then
   passes untouched, which is the signal that the migration was UI-only.
4. Status strip + folded stopped engines (§6.1, §6.2) — the two changes with the
   best visible-effect-to-risk ratio.
5. Extract `app/ui/` (§7).
6. Retire whatever the audit condemned (§5), now one registry line each.
7. Nest tunables under engines, split the Engines tab, collapse Quick Trade
   (§6.3–§6.5) — after the audit says which of these still earn their space.

Steps 2–5 change no trading behaviour at all. Step 6 is the only one that needs
a careful look at the paper/live isolation rule (CLAUDE.md rule 4) and the
one-band-cron rule (rule 6) on the way out.
