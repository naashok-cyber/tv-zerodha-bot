# Session Handoff — 2026-04-26 (weekend pause)

## State: P0 complete. P1-a pre-flight done. Pine Script shipped. Resume Apr 28.

**Resume rule: do NOT start P1-a coding until the weekly budget resets on 2026-04-28 morning.**

---

## What Was Done This Session

| Item | Commit | Notes |
|---|---|---|
| P1-a pre-flight | `e30e4b7` | scipy, py_vollib, py_lets_be_rational installed & sanity-tested |
| Pine Script library | `744b4d8` | `pinescript/alert_emitter.pine`, `example_strategy.pine`, `README.md` |

**P1-a pre-flight result:** `delta('c', 100, 100, 0.1, 0.05, 0.2)` → `0.5441` (correct BSM value; user's "~0.53" was approximate). All three libs resolved from existing venv wheels — no build from source needed.

**Pine Script:** `AlertEmitter` library + 9/21 EMA crossover strategy. JSON schema in `alert_emitter.pine` matches `app/webhook_models.py` exactly (`tv_ticker`, `tv_exchange`, `action`, `order_type`, `entry_price`, `stop_loss`, `sl_percent`, `atr`, `quantity_hint`, `product`, `"time"` alias → `{{timenow}}`, `bar_time`, `interval`). Nullable fields serialize to JSON `null`.

---

## P0 Build Summary

| Phase | Commit | Key files | Cumulative tests |
|---|---|---|---|
| P0-a | `4946265` | config.py, storage.py | 128 |
| P0-b | `8cda702` | auth.py, kite_session.py | 128 |
| P0-c | `40fc24b` | symbol_mapper.py, Instrument model | 128 |
| PBKDF2 600k | `65ae639` | config.py, .env.example | 128 |
| P0-d | `5f97ea1` | orders.py, watcher.py | 149 |
| P0-e | `f0564bc` | main.py, webhook_models.py | 160 |

**160 tests passing. P0 is complete.**

---

## Current Commit State (15 commits on master)

```
744b4d8 Pine Script alert emitter library and example strategy
e30e4b7 P1-a pre-flight: install Greeks libraries
55051f4 P0 milestone: handoff updated, P1 deferred to fresh week
c0aceca P0 complete: handoff updated for P1 entry
e8a7e40 Update handoff: P0-e complete, P0 done, P1 next
f0564bc P0-e: FastAPI main module wiring webhook + callback + dashboard
0a84dec Update handoff: P0-d complete, P0-e next
5f97ea1 P0-d: orders and watcher modules with tests
65ae639 security: bump PBKDF2 iterations to 600k per OWASP guidance
c61ced6 Update handoff: pause after P0-c, P0-d next
40fc24b P0-c: symbol_mapper module with Instrument model and tests
8cda702 P0-b: auth and kite_session modules with tests
4946265 P0-a: config and storage modules with tests
724ea14 Pause point: handoff and resume notes
d5497e6 Initial: spec files and prototype
```

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
| Rate limiter | In-process token bucket (not slowapi) | P0-b |
| PBKDF2 iterations | **600,000** (applied pre-P0-d) | P0-d |
| round_to_tick | ROUND_HALF_UP — revisit if NSE rejects half-tick boundaries | P0-c |
| backoff_call | Max 3 retries, 1s base, 8s cap; retry NetworkException + code==429 | P0-d |
| place_entry lot rejection | Raises ValueError (not silent None) | P0-d |
| GTT leg order | SL = leg 0, target = leg 1 (Kite convention) | P0-d |
| risk.py | Absorbed into P1-d (after strike_selector) | P0-e |
| Greeks libs | py_vollib 1.0.1 + py_lets_be_rational 1.0.1 + scipy 1.17.1 | P1-a pre-flight |

---

## Open Items Carrying Into P1

1. **Watcher startup (deferred from P0-e):** `OrderWatcher` is created in lifespan but NOT
   started (no Kite token at cold boot). Wire `watcher.start()` inside `handle_callback()` in
   `kite_session.py` — the moment the daily token lands is the natural hook. Do this in P1-a
   or wherever it fits first.

2. **Background task stubs (main.py `_process_alert`):** All non-trivial paths currently log
   "not wired in P0-e, deferred to P1". P1-f replaces these stubs with the real
   greeks → expiry → strike → risk → entry flow.

3. **risk.py (P1-d):** Premium-based sizing and daily/consecutive loss caps both depend on the
   strike selector returning a real option. Do not write risk.py before strike_selector.py.

---

## Next Phase: P1-a — greeks.py

### What P1-a covers

- **IV back-solver**: given market premium + strike + expiry → implied volatility (brentq /
  Newton root-find on BSM/Black-76 price)
- **Black-Scholes delta** for NSE index & equity options (European; use BSM with continuous
  dividend yield for index carry)
- **Black-76 delta** for MCX commodity options (futures-based underlier; Black's 1976 model)
- Thin wrapper so callers pass an `Instrument` + `ltp` + `spot/futures_price` and get back
  a `GreeksResult(delta, gamma, theta, iv)`

### Pre-flight already done

- scipy 1.17.1, py_vollib 1.0.1, py_lets_be_rational 1.0.1 installed and in requirements.txt.
- Sanity test confirmed: `delta('c', 100, 100, 0.1, 0.05, 0.2)` → 0.5441 ✓

### Reference test values

Delta validation reference values are in **v2 §9** of
`tradingview_zerodha_bot_prompt_v2_options.md`. Read that section before writing tests — do
not invent test deltas from scratch.

### P1 build order (subject to user approval at each step)

| Phase | Module | Key responsibility |
|---|---|---|
| P1-a | `greeks.py` | IV back-solver, BS delta (NSE), Black-76 delta (MCX) |
| P1-b | `expiry_resolver.py` | Nearest weekly/monthly selection, day-cutoff logic |
| P1-c | `strike_selector.py` | Delta-targeted strike, OI/spread/premium filters |
| P1-d | `risk.py` | Premium sizing, daily loss cap, consecutive-loss circuit breaker |
| P1-e | `notifier.py` | Telegram send, no-op fallback |
| P1-f | Pipeline wiring | Replace `_process_alert` stubs; wire watcher GTT on EntryFilledEvent |

---

## What To Do When You Resume (Apr 28+)

1. Open PowerShell, `cd C:\Users\srias\tv-zerodha-bot`
2. Confirm Python: `py -3.11 --version`
3. Activate venv: `.\.venv\Scripts\Activate.ps1`
4. Confirm tests still pass: `.\.venv\Scripts\pytest -q` (expect 160 passed)
5. Start Claude Code: `claude`
6. Tell Claude: **"Resume from SESSION_HANDOFF.md. Weekly budget reset. Proceed with P1-a."**

---

## Proposed Full File Tree (reference)

```
tv-zerodha-bot/
├── app/
│   ├── config.py, storage.py                             ← P0-a done
│   ├── auth.py, kite_session.py                          ← P0-b done
│   ├── symbol_mapper.py                                  ← P0-c done
│   ├── orders.py, watcher.py                             ← P0-d done
│   ├── main.py, webhook_models.py                        ← P0-e done
│   ├── greeks.py                                         ← P1-a next
│   ├── expiry_resolver.py                                ← P1-b
│   ├── strike_selector.py                                ← P1-c
│   ├── risk.py                                           ← P1-d
│   ├── notifier.py                                       ← P1-e
│   └── templates/                                        ← P2
├── tests/
│   └── fixtures/instruments_sample.csv
├── infra/  (Terraform)                                   ← P2
├── pinescript/                                           ← done (alert emitter + EMA strategy)
├── simulation_mode.py                                    ← P2
├── Dockerfile, docker-compose.yml, Caddyfile             ← P2
├── .env.example, requirements.txt, pyproject.toml
└── .github/workflows/ci.yml                              ← P2
```
