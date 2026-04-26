# Session Handoff — 2026-04-26 (P0 complete)

## State: P0 complete. 160 tests passing. P1 not started.

---

## P0 Build Summary

| Phase | Commit | Files | Tests |
|---|---|---|---|
| P0-a | `4946265` | config.py, storage.py | 128 → 128 |
| P0-b | `8cda702` | auth.py, kite_session.py | 128 |
| P0-c | `40fc24b` | symbol_mapper.py, Instrument model | 128 |
| PBKDF2 bump | `65ae639` | config.py, .env.example (100k→600k) | 128 |
| P0-d | `5f97ea1` | orders.py, watcher.py | 149 |
| P0-e | `f0564bc` | main.py, webhook_models.py | 160 |
| Handoff | `e8a7e40` | SESSION_HANDOFF.md | — |

---

## Current Commit State

```
git log --oneline:

e8a7e40  Update handoff: P0-e complete, P0 done, P1 next
f0564bc  P0-e: FastAPI main module wiring webhook + callback + dashboard
5f97ea1  P0-d: orders and watcher modules with tests
65ae639  security: bump PBKDF2 iterations to 600k per OWASP guidance
c61ced6  Update handoff: pause after P0-c, P0-d next
40fc24b  P0-c: symbol_mapper module with Instrument model and tests
8cda702  P0-b: auth and kite_session modules with tests
4946265  P0-a: config and storage modules with tests
724ea14  Pause point: handoff and resume notes
d5497e6  Initial: spec files and prototype
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
| PBKDF2 iterations | **600,000** (applied in pre-flight before P0-d) | P0-d |
| round_to_tick | ROUND_HALF_UP — revisit if NSE rejects half-tick boundaries | P0-c |
| backoff_call | Max 3 retries, 1s base, 8s cap; retry NetworkException + code==429 | P0-d |
| place_entry lot rejection | Raises ValueError (not silent None) | P0-d |
| GTT leg order | SL = leg 0, target = leg 1 (Kite convention) | P0-d |
| risk.py | Absorbed into P1 (P1-d); premium sizing needs real strike from selector | P0-e |

---

## Open Notes Carrying Into P1

1. **Watcher startup**: `OrderWatcher` is created in lifespan but NOT started (no token at cold boot).
   Wire `watcher.start()` inside `handle_callback()` in `kite_session.py` — the moment the daily
   token lands is the right place to bring the websocket live. Do this in P1-a or wherever it fits
   naturally.

2. **risk.py in P1**: Will be P1-d (after greeks.py, expiry_resolver.py, strike_selector.py).
   Premium-based sizing and daily/consecutive caps both depend on strike selector output and the
   full order-placement path that P1 wires up.

3. **Background task stubs**: All non-trivial paths in `_process_alert` (main.py) currently log
   "not wired in P0-e, deferred to P1". P1 replaces these stubs with real pipeline calls.

---

## Next Phase: P1-a — greeks.py

P1 build order (subject to approval at each phase):
- **P1-a**: `app/greeks.py` — Black-Scholes / Black-76 delta, gamma, theta, IV solver
- **P1-b**: `app/expiry_resolver.py` — nearest weekly/monthly selection with day-cutoff logic
- **P1-c**: `app/strike_selector.py` — delta-targeted strike selection with OI/spread/premium filters
- **P1-d**: `app/risk.py` — premium sizing, daily loss cap, consecutive-loss circuit breaker
- **P1-e**: `app/notifier.py` — Telegram send with no-op fallback
- **P1-f**: Pipeline wiring in `main.py` — replace stubs with real greeks→expiry→strike→risk→entry flow; wire watcher GTT placement on EntryFilledEvent

Do NOT start P1-a until user approves.

---

## What To Do When You Resume

1. Open PowerShell, `cd C:\Users\srias\tv-zerodha-bot`
2. Confirm Python: `py -3.11 --version`
3. Activate venv: `.\.venv\Scripts\Activate.ps1`
4. Confirm tests still pass: `.\.venv\Scripts\pytest -q` (expect 160 passed)
5. Start Claude Code: `claude`
6. Tell Claude: **"Resume from SESSION_HANDOFF.md. Proceed with P1-a."**

---

## Proposed Full File Tree (reference — updated)

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
│   └── templates/  (dashboard.html, positions.html, history.html)  ← P2
├── tests/
│   └── fixtures/instruments_sample.csv
├── infra/  (Terraform)                                   ← P2
├── pinescript/                                           ← P2
├── simulation_mode.py                                    ← P2
├── Dockerfile, docker-compose.yml, Caddyfile             ← P2
├── .env.example, requirements.txt, pyproject.toml
└── .github/workflows/ci.yml                             ← P2
```
