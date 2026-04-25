# Session Handoff — 2026-04-25

## State: P0-b complete. P0-c not started.

---

## Last Completed Phase: P0-b

**Files added / modified in P0-b (committed as part of this session):**

| File | Status |
|---|---|
| `app/auth.py` | new — IP allowlist, HMAC verify, token-bucket rate limiter |
| `app/kite_session.py` | new — Fernet encryption, PBKDF2 key derivation, TokenStaleError, OAuth callback |
| `tests/test_auth.py` | new — 33 tests; IP matrix, HMAC edge cases, rate-limit trip |
| `tests/test_kite_session.py` | new — 22 tests; token round-trip, age checks, OAuth callback |
| `app/config.py` | modified — added SECRET_KEY, PBKDF2_ITERATIONS, WEBHOOK_RATE_LIMIT_PER_MINUTE |
| `.env.example` | modified — added encryption and rate-limit fields |
| `requirements.txt` | modified — pinned kiteconnect==5.2.0, cryptography==47.0.0 + transitive deps |

**Test result:** 111 passed, 0 failed, 3.63s (Python 3.11.9, pytest 8.4.2)

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
| Rate limiter | In-process token bucket (not slowapi; no ASGI dep yet) | P0-b |
| PBKDF2 iterations | **100,000** (current) — see note below | P0-b |

---

## PBKDF2 Iterations — Queued Change

**Status: NOT YET APPLIED.**

User asked at end of session whether the bump to **600,000** was applied or queued.
It was queued — the value in `app/config.py` and `.env.example` is currently `100_000`.

**Action on resume:** if you want 600,000, run:
```
# In app/config.py, change:
PBKDF2_ITERATIONS: int = 100_000
# to:
PBKDF2_ITERATIONS: int = 600_000

# In .env.example, change:
PBKDF2_ITERATIONS=100000
# to:
PBKDF2_ITERATIONS=600000
```
Note: higher iterations slow the daily-login callback by ~500ms on a t3.small (acceptable).
All existing kite_session tests use `PBKDF2_ITERATIONS=1` so no test changes needed.

---

## Current Commit State

```
git log --oneline (after this session's commits):

<commit-hash>  P0-b: auth, kite_session, tests
4946265        P0-a: config and storage modules with tests
724ea14        Pause point: handoff and resume notes
d5497e6        Initial: spec files and prototype
```

---

## Next Phase: P0-c

**Scope: `app/symbol_mapper.py` + `tests/test_symbol_mapper.py`**

The user will provide a detailed instruction block on resume (same format as P0-b).
The known requirements from v1/v2/v3 specs are captured below as the working spec.

---

### P0-c Working Spec (from v1 §5 + v2 §1/§2 + v3 P0)

#### symbol_mapper.py responsibilities

1. **Instruments download**
   - Download `https://api.kite.trade/instruments` (CSV, ~40 MB) into the local SQLite
     `instruments` table at 08:30 IST daily (triggered by scheduler; manually callable).
   - Columns to store: `instrument_token`, `exchange_token`, `tradingsymbol`, `name`,
     `expiry`, `strike`, `tick_size`, `lot_size`, `instrument_type`, `segment`, `exchange`.

2. **`resolve_underlying(tv_ticker) -> Underlying`** (v2 addition)
   - Returns a structured object: `{name, segment, is_natural_gas, spot_source}`.
   - Used by the signal router to decide whether to trade an option or a future.
   - NATURALGAS detection: match `name` in `settings.NATURAL_GAS_NAMES` — never a
     string-match on the raw TV ticker.

3. **`resolve(tv_ticker, tv_exchange, hint=None) -> KiteInstrument`** (v1 core)
   Covers these cases:

   | TradingView input | Kite result | Notes |
   |---|---|---|
   | `NSE:RELIANCE` | `RELIANCE` on NSE | strip prefix |
   | `NSE:NIFTY` (index) | not orderable — resolve via hint | needs `hint.instrument: FUT or OPT` |
   | `NSE:BANKNIFTY` | same | |
   | FUT monthly | `NIFTY26JANFUT` on NFO | format: SYMBOL+YY+MMM+FUT |
   | OPT weekly | `NIFTY2611323500CE` on NFO | format: SYMBOL+YY+M+DD+STRIKE+CE/PE |
   | OPT monthly | `NIFTY26JAN23500CE` on NFO | format: SYMBOL+YY+MMM+STRIKE+CE/PE |
   | `MCX:CRUDEOIL1!` | `CRUDEOIL<YY><MON>FUT` on MCX | resolve continuous → near-month |
   | NATURALGAS | near-month FUT on MCX | routes to future, not option |

4. **Index aliasing for `kite.ltp()` / `kite.quote()`**
   Hard-coded dict (these do not appear in instruments master):
   ```python
   {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE"}
   ```

5. **Tick-size rounding** — `round_to_tick(price, tick_size) -> Decimal`
   Use `Decimal`, never `float`. NSE options tick = 0.05; MCX varies.

6. **Lot-size enforcement** — quantity must be a multiple of `lot_size`; round down, never up.

7. **Expiry selection** (`expiry_resolver.py` is P1, but symbol_mapper needs the table) —
   Derive distinct expiries per underlying from the instruments table; the resolver will
   consume them.

8. **Fallback** — if resolution fails: log to `errors` table, send Telegram alert (no-op if
   token missing), return `None` (caller maps to HTTP 422).

#### Test requirements for P0-c

- All offline; instruments table populated from a fixture CSV (no live network).
- 20+ symbol-resolution cases: equity, FUT monthly, OPT weekly, OPT monthly, MCX, indices.
- Tick-size rounding: verify `Decimal` arithmetic, not float.
- Lot-size enforcement: quantity rounding down.
- `resolve_underlying` correctly flags NATURALGAS variants as `is_natural_gas=True`.
- Resolution failure returns `None` (no exception propagation to caller).
- Index alias dict covers all four index names.

#### Rules (same as P0-a / P0-b)
- `_env_file=None` in test settings
- Type hints everywhere
- IST timezone awareness for any expiry date logic
- No live network in any test
- Stop after pytest is green
- Show test output and any decisions beyond spec
- Do NOT start P0-d until user approves P0-c

---

## What To Do When You Resume

1. Open PowerShell, `cd C:\Users\srias\tv-zerodha-bot`
2. Confirm Python: `py -3.11 --version`
3. Activate venv: `.\.venv\Scripts\Activate.ps1`
4. Confirm tests still pass: `.\.venv\Scripts\pytest -v`
5. Start Claude Code: `claude`
6. Open this file and the v1/v2/v3 specs if Claude doesn't have context.
7. Tell Claude: **"Resume from SESSION_HANDOFF.md. Apply PBKDF2=600000 if approved,
   then proceed with P0-c."** (or skip the PBKDF2 change and go straight to P0-c).

---

## Proposed Full File Tree (reference — unchanged from P0-a handoff)

```
tv-zerodha-bot/
├── app/
│   ├── main.py, auth.py, config.py, kite_session.py   ← P0-a/b done
│   ├── symbol_mapper.py                                ← P0-c
│   ├── expiry_resolver.py, greeks.py, strike_selector.py  ← P1
│   ├── risk.py, orders.py, watcher.py                 ← P0 (remaining)
│   ├── storage.py, notifier.py, scheduler.py          ← P0-a done / P2
│   └── templates/  (dashboard.html, positions.html, history.html)
├── tests/
├── infra/  (Terraform)
├── pinescript/
├── simulation_mode.py
├── Dockerfile, docker-compose.yml, Caddyfile
├── .env.example, requirements.txt, pyproject.toml
└── .github/workflows/ci.yml
```
