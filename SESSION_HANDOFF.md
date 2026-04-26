# Session Handoff — 2026-04-28 (pause after P1-b)

## State: P0 + P1-a + P1-b complete. 199 tests passing. Resume P1-c after weekly reset.

**Resume rule: do NOT start P1-c until weekly budget resets. Next window: 2026-04-28 02:30 IST.**

---

## What Was Done This Session

| Item | Commit | Notes |
|---|---|---|
| P1-a pre-flight | `e30e4b7` | scipy, py_vollib, py_lets_be_rational installed & sanity-tested |
| Pine Script library | `744b4d8` | `pinescript/alert_emitter.pine`, `example_strategy.pine`, `README.md` |
| P1-a: greeks.py | `9d6af16` | BSM + Black-76 dispatchers; 17 tests |
| P1-b: expiry_resolver + strike_selector | `6f60fa9` | Audit log, guardrails, tie-breaker; 22 tests |

---

## Full Build Summary

| Phase | Commit | Key files | Cumulative tests |
|---|---|---|---|
| P0-a | `4946265` | config.py, storage.py | 128 |
| P0-b | `8cda702` | auth.py, kite_session.py | 128 |
| P0-c | `40fc24b` | symbol_mapper.py, Instrument model | 128 |
| PBKDF2 600k | `65ae639` | config.py, .env.example | 128 |
| P0-d | `5f97ea1` | orders.py, watcher.py | 149 |
| P0-e | `f0564bc` | main.py, webhook_models.py | 160 |
| P1-a pre-flight | `e30e4b7` | requirements.txt | 160 |
| Pine Script | `744b4d8` | pinescript/ | 160 |
| P1-a | `9d6af16` | greeks.py | 177 |
| P1-b | `6f60fa9` | expiry_resolver.py, strike_selector.py | 199 |

**199 tests passing. 18 commits on master.**

---

## Current Commit State

```
6f60fa9 P1-b: expiry resolver and strike selector with audit log
9d6af16 P1-a: greeks module with BSM and Black-76 dispatchers
c8a039c Weekend pause: P0 + P1 pre-flight + Pine Script done
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
| BSM for NSE | `py_vollib.black_scholes_merton`; q comes LAST in delta signature | P1-a |
| Black-76 for MCX | `py_vollib.black`; IV has r-before-t; delta has t-before-r | P1-a |
| B76 IV naming | py_vollib calls it "discounted_option_price" but accepts raw price | P1-a |
| StrikeDecision.alert_id | Nullable — select_strike usable without a live Alert row | P1-b |
| Session close times | NSE 15:30 IST, MCX 23:30 IST (for time_to_expiry calc) | P1-b |
| Expiry cutoffs | NSE 14:30 IST, MCX 22:00 IST (skip-expiry-day roll) | P1-b |
| Missing depth | Skip spread check with warning; do NOT reject the strike | P1-b |
| Chunking | kite.quote() calls chunked at 500 instruments; path untested >500 | P1-b |

---

## Open Items Carrying Into P1-c

1. **Watcher startup (deferred from P0-e):** `OrderWatcher` is created in lifespan but NOT
   started. Wire `watcher.start()` inside `handle_callback()` the moment the daily token lands.
   **Do this first in P1-c before any pipeline wiring.**

2. **Background task stubs (`_process_alert` in main.py):** All non-trivial paths log
   "not wired". P1-c replaces these stubs with the real signal-routing flow.

3. **risk.py (now part of P1-c scope):** Premium-based sizing and loss-cap guards are needed
   inline in P1-c pipeline; do NOT write as a separate module first — inline them or co-locate.

See `app/TECH_DEBT.md` for tracked deferred issues.

---

## Next Phase: P1-c — Signal routing + risk guards

### What P1-c covers

**Signal routing in `_process_alert` (main.py):**
- BUY signal → buy CE option (resolve_expiry → select_strike → place_entry)
- SELL signal → buy PE option (same flow, flag="PE")
- NATURALGAS → route to near-month future via `_resolve_continuous()` → `place_entry`
- Wire `watcher.start()` in `handle_callback()` as first task

**Risk guards (inline, not a separate module until later):**
- PREMIUM_BASED sizing: `quantity = floor(CAPITAL_PER_TRADE / (option_ltp * lot_size))`
- Daily-loss cap: reject new entries if unrealised + realised loss today ≥ `effective_max_daily_loss`
- Max trades per day: reject if `Alert` rows today ≥ `MAX_TRADES_PER_DAY`
- Max open positions: reject if open `Position` count ≥ `MAX_OPEN_POSITIONS`
- Consecutive-losses circuit breaker: reject if consecutive SL-hits ≥ `CONSECUTIVE_LOSSES_LIMIT`

**After the trade entry:**
- Persist `Position` row
- Place GTT OCO via `place_gtt_oco()`
- Wire watcher to listen for fills on that order ID

### Reference

Risk sizing spec: v2 §5, v3 §1 of `tradingview_zerodha_bot_prompt_v2_options.md`
and `tradingview_zerodha_bot_prompt_v3_gap_analysis.md`.

### P1 remaining build order

| Phase | Module | Key responsibility |
|---|---|---|
| P1-c | main.py `_process_alert` + risk guards | Full signal-to-order pipeline |
| P1-d | notifier.py | Telegram send, no-op fallback |
| P1-e | Pipeline hardening | Error handling, Telegram alerts on every failure path |

---

## What To Do When You Resume

1. Open PowerShell, `cd C:\Users\srias\tv-zerodha-bot`
2. Confirm Python: `py -3.11 --version`
3. Activate venv: `.\.venv\Scripts\Activate.ps1`
4. Confirm tests still pass: `.\.venv\Scripts\pytest -q` (expect 199 passed)
5. Start Claude Code: `claude`
6. Tell Claude: **"Resume from SESSION_HANDOFF.md. Weekly budget reset. Proceed with P1-c."**

---

## Proposed Full File Tree (reference)

```
tv-zerodha-bot/
├── app/
│   ├── config.py, storage.py                             ← P0-a done
│   ├── auth.py, kite_session.py                          ← P0-b done
│   ├── symbol_mapper.py                                  ← P0-c done
│   ├── orders.py, watcher.py                             ← P0-d done
│   ├── main.py, webhook_models.py                        ← P0-e done (stubs to fill in P1-c)
│   ├── greeks.py                                         ← P1-a done
│   ├── expiry_resolver.py, strike_selector.py            ← P1-b done
│   ├── notifier.py                                       ← P1-d
│   ├── TECH_DEBT.md                                      ← created this session
│   └── templates/                                        ← P2
├── tests/
│   └── fixtures/instruments_sample.csv
├── infra/  (Terraform)                                   ← P2
├── pinescript/                                           ← done (alert emitter + EMA strategy)
├── simulation_mode.py                                    ← P2
├── Dockerfile, docker-compose.yml, Caddyfile             ← P2
├── .env.example, requirements.txt, pyproject.toml
└── .github/workflows/ci.yml                             ← P2
```
