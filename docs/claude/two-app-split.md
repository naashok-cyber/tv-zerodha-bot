# Splitting `/control` into two apps

Goal: two separately-installable home-screen apps — a lean **production trading
surface** you open under pressure, and a **lab** for data, analysis and tuning.

The one-sentence answer: **split the front door, not the runtime.** Two PWAs on
one origin served by one FastAPI process. Everything below is why, and what it
costs.

---

## 1. Three ways to split, and which one is safe

| | What it means | Verdict |
|---|---|---|
| **A. Two front doors, one process** | Two manifests, two shells, two route groups, one uvicorn | **Recommended** |
| **B. Two processes, one image** | `ROLE=trade` (writer, owns crons) + `ROLE=lab` (read-only) | Possible later, read-only Lab only |
| **C. Two services / two repos** | Separate deploys, separate DBs | No |

Option A gives you the thing you actually asked for — two icons, two apps, two
scopes of concern — at near-zero risk. B is a *deployment* change that buys
isolation, and it is only safe once Lab is provably read-only. C throws away the
shared DB and session for nothing.

## 2. Why not two processes (yet) — the hard dependencies

These are all single-instance-by-construction today. A second process running
the same `app/main.py` duplicates every one of them:

1. **APScheduler.** `make_scheduler()` + `_scheduler.start()` run in `lifespan`
   (`main.py:1206`). Two processes = two of every cron: two band-hedge ticks,
   two EOD squareoffs, two scheduled straddles. This is CLAUDE.md rule 6
   violated at the process level, and it places real duplicate orders.
2. **The Kite quote bucket.** `orders.throttled_quote_call` is an *in-process*
   throttle. Two processes = two independent buckets against one 1 req/sec
   account limit. The 429s land on whichever caller is unlucky — including the
   trading path.
3. **The tick stream.** One `OrderWatcher` WebSocket fans out to
   `TrailingSlManager` *and* `OrderFlowRecorder` (`main.py:1176-1186`). Trailing
   SL state lives in that process. A second ticker means split-brain trailing.
4. **`app/state.py` overrides.** These are module-level globals persisted to
   `data/state_overrides.json`, loaded once at startup. A second process holds
   its own copy: toggling paper mode in Lab would write the file but the trading
   process would never re-read it. Silent, and dangerous in exactly the wrong
   direction.
5. **SQLite.** Already `journal_mode=WAL` (`storage.py:473`) — concurrent readers
   with one writer. Fine for a read-only Lab, not for two writers.

For option B, Lab must therefore: not start the scheduler, not start the
watcher, never call a `state.set_*`, never place an order. That is close to true
already — but it is an audit, not an assumption.

## 3. The precedent already in the repo

`app/commodity_agents/` is **already a second PWA**: its own
`/commodity-agents/manifest.json` (`dashboard.py:651`), its own `/sw.js`, its own
`start_url`, its own shell, sharing the `zb_session` cookie via `_page_gate`
(`routes.py:79`). It works today. The split is generalising that, not inventing
it.

## 4. Where the boundary actually falls today (it crosses)

Two dependencies run the wrong way and must be resolved before the split reads
cleanly:

- **Home's open-positions card fetches `/commodity-agents/portfolio-greeks`**
  (`main.py:2761`). Live position greeks are Trade data living behind a Lab
  prefix.
- **The whole Markets tab is Lab data.** `_VOL_GRID_JS` sets `var CA =
  '/commodity-agents'` (`main.py:3021`) and every series, recommendation and
  decision it renders comes from there. Markets is an analysis surface that
  happens to sit inside the production page.

## 5. The proposed split

**Boundary rule** (so the next feature is decidable without a meeting):

> Can it place, modify or cancel an order, or change what the bot does in the
> next 60 seconds? → **Trade.** Otherwise → **Lab.**

### App 1 — ZeroBot **Trade** · `/control` · production

Loads fast, works on a bad connection, shows state and lets you stop things.

- Home: P&L hero, risk meters, session/Kite validity, next job
- Emergency stop · paper/live · trade mode
- Open positions, Orders, GTTs, square-off
- Quick Trade / voice
- Engine on/off switches, risk caps

Routes: `/control`, `/api/control/summary`, `/control/*` toggles, `/orders`,
`/gtts`, `/control/voice/*`, plus the non-UI `/webhook`, `/kite/*`, `/healthz`.

### App 2 — ZeroBot **Lab** · `/lab` · analysis

- Markets volatility grid (moves out of Trade)
- Order Flow footprint
- History, scorecard, journal, calibration
- Raw TradingView alert log
- Commodity Agents dashboard / analyze / desk
- Hedge tunable drawers (numeric thresholds)

Routes: `/history`, `/orderflow`, `/api/orderflow/*`, `/dashboard`,
`/commodity-agents/*`, `/control/hedge/{slug}`.

**The gray zone is hedge tunables.** They change behaviour, so rule-wise they are
Trade — but you tune them off a chart, not in a panic. Recommendation: the
engine's **on/off toggle stays in Trade**, the **numeric drawers move to Lab**,
next to the data that justifies the number.

## 6. Dependencies — the actual work list

1. **Two manifests, distinct `id`.** Add `"id"` to both (`/control` and `/lab`);
   without it Chrome/Android can treat a second same-origin install as a
   *replacement* for the first. iOS keys off the page's linked manifest, so two
   pinned icons work once each shell links its own.
2. **Keep one service worker.** A SW is per-origin, and `/sw.js` is registered
   with scope `/` from `_shell()`. Its cache list —
   `['/control','/orders','/gtts','/history','/dashboard']` (`main.py:2276`) —
   straddles the split; update the list, don't add a second SW at a nested path
   (a SW at `/lab/sw.js` gets scope `/lab/` and could not cache anything else
   without a `Service-Worker-Allowed` header).
3. **Parameterise `_shell()`.** It currently hardcodes the wordmark, one CSS
   blob, and four tabs pointing at `/control#hash` (`main.py:4114-4137`).
   Replace with an `AppShell` descriptor — name, manifest href, theme colour,
   accent, tab list — and render both apps from it. Same registry trick as
   `hedge_tunables.ENGINES`. ~40 lines, and it is what makes this a split rather
   than a fork. **Preserves CLAUDE.md rule 11**: tab bar still renders before
   `.wrap`.
4. **One origin, one login — do not split by hostname.** `zb_session` is set
   host-only (no `domain=`), `secure`, `samesite=strict`, `path=/`
   (`webauthn_routes.py:42`), and is read by both `_auth_guard` (`main.py:1286`)
   and `_page_gate`. Path-split → one sign-in covers both apps, zero auth work.
   Hostname-split → two logins, a second cert, a `domain=` cookie, and
   `samesite=strict` breaking the cross-host redirect. Large cost, no benefit.
5. **Move `portfolio-greeks` to a neutral path** (`/api/positions/greeks`),
   aliasing the old one so `tests/test_*` and the agents desk keep working.
   Trade must not depend on a Lab prefix.
6. **Delete the `More` tab.** It is a link list (`main.py:5428`) to what becomes
   Lab. Replace with a single Lab link; Lab gets a Trade link back.
7. **Extract `app/ui/`** — `shell.py`, `trade.py`, `lab.py`, `components.py`.
   Do this *first*: 63% of `main.py` is UI and the split is unreviewable inside
   a 6,261-line file.
8. **Caddy: nothing.** Path-split needs no proxy change at all.
9. **Tests: cheap if paths hold.** `tests/` asserts on paths, not panels —
   `/control`, `/api/control/summary`, `/orders`, `/gtts`, `/orderflow`,
   `/history`, `/dashboard`, `/portfolio-greeks`. Keep the paths (or alias) and
   the suite stays green.

## 7. Sequencing

| Phase | Work | Trading behaviour |
|---|---|---|
| 0 | Extract `app/ui/` | unchanged |
| 1 | `AppShell` descriptor; still one app | unchanged |
| 2 | Stand up `/lab` — own manifest, own shell; move History, Alerts, Order Flow, Markets, agents | unchanged |
| 3 | Strip Trade to Home + books + engines + risk; delete `More` | unchanged |
| 4 | Move `portfolio-greeks` to a neutral path | unchanged |
| 5 | *Optional, later:* run Lab as a second container (`ROLE=lab`, no scheduler, no watcher, read-only) | needs §2 audit |

Phases 0–4 change no trading behaviour whatsoever.

## 8. Run the usage audit first

Splitting a dormant feature into a second app is worse than deleting it — you
carry the code *and* end up with a Lab full of things you never open. Run

```
docker compose exec bot python scripts/audit_feature_usage.py --days 120
```

before phase 2, and retire what it shows dead (see
`docs/claude/control-simplification.md` §5) rather than relocating it.
