# Session Handoff — 2026-04-27 (pause after P1-d)

## State: P0 + P1-a + P1-b + P1-c + P1-d complete. 234 tests passing.

---

## What Was Done This Session

| Item | Commit | Notes |
|---|---|---|
| P1-c: signal routing | `4d101fa` | BUY/SELL/EXIT/TRAIL fully wired; GTT OCO placed on fill |
| P1-d: risk module | `28e473e` | risk.py + sizing centralization; 19 new tests |

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
| P1-c | `4d101fa` | main.py `_process_alert` full pipeline | 215 |
| P1-d | `28e473e` | risk.py, config.py, watcher.py, main.py | 234 |

**234 tests passing. 21 commits on master.**

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
| Greeks libs | py_vollib 1.0.1 + py_lets_be_rational 1.0.1 + scipy 1.17.1 | P1-a pre-flight |
| BSM for NSE | `py_vollib.black_scholes_merton`; q comes LAST in delta signature | P1-a |
| Black-76 for MCX | `py_vollib.black`; IV has r-before-t; delta has t-before-r | P1-a |
| B76 IV naming | py_vollib calls it "discounted_option_price" but accepts raw price | P1-a |
| StrikeDecision.alert_id | Nullable — select_strike usable without a live Alert row | P1-b |
| Session close times | NSE 15:30 IST, MCX 23:30 IST (for time_to_expiry calc) | P1-b |
| Expiry cutoffs | NSE 14:30 IST, MCX 22:00 IST (skip-expiry-day roll) | P1-b |
| Missing depth | Skip spread check with warning; do NOT reject the strike | P1-b |
| Chunking | kite.quote() calls chunked at 500 instruments; path untested >500 | P1-b |
| risk.py gate order | check_risk_gates at top of _process_alert; skipped for DRY_RUN | P1-d |
| risk sizing | compute_option_qty / compute_futures_qty; all Decimal inputs | P1-d |
| RISK_PCT config | New Decimal field = 0.01 (1% fraction) for futures sizing | P1-d |
| SL_PERCENT config | New Decimal field = 0.005 (0.5%) — NG futures SL default | P1-d |
| MAX_DAILY_LOSS config | New Decimal field = 2000 (₹ absolute) for risk.py | P1-d |
| GttFilledEvent | Fires on any COMPLETE SELL order not in _watched_order_ids | P1-d |
| record_trade_result | Does NOT commit; caller (main.py) commits | P1-d |

---

## Open Tech Debt

See `app/TECH_DEBT.md` for tracked deferred issues (TD-1 through TD-4).

Additional debt logged in P1-d:
- TD-5: `check_risk_gates` also blocks EXIT/TRAIL alerts (intentional per spec, but consider relaxing for exit paths before production)
- TD-6: `GttFilledEvent` fires on any COMPLETE SELL — could catch manual broker orders that aren't GTT exits; add symbol-level matching before production

---

## Next Phase: P1-e — Notifier + pipeline hardening

### What P1-e covers
- `notifier.py`: Telegram send, no-op if token missing (never crash)
- Error handling on every failure path (log + notify)
- AppError rows written on every unhandled exception in _process_alert

### P1 remaining build order

| Phase | Module | Key responsibility |
|---|---|---|
| P1-e | notifier.py + hardening | Telegram alerts on every failure path |

---

## What To Do When You Resume

1. Open PowerShell, `cd C:\Users\srias\tv-zerodha-bot`
2. Confirm Python: `py -3.11 --version`
3. Activate venv: `.\.venv\Scripts\Activate.ps1`
4. Confirm tests still pass: `.\.venv\Scripts\pytest -q` (expect 234 passed)
5. Start Claude Code: `claude`
6. Tell Claude: **"Resume from SESSION_HANDOFF.md. Proceed with P1-e."**
