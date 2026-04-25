# Session Handoff — 2026-04-23

## State: Pre-code. No source files written yet.

---

## 5-Bullet Summary

1. **Specs fully read; architecture agreed.** All three spec versions (v1 base, v2 options/delta, v3 gap-analysis) and the Flask prototype were read in full. The build plan is: FastAPI webhook receiver → TradingView signal router (BUY→CE, SELL→PE, NATURALGAS→future) → delta-based strike selector (δ≈0.65, py_vollib/Black-76) → Kite order executor with GTT OCO exits → broker-side breakeven/trailing via GTT modify → SQLite-backed audit log and dashboard.

2. **Priority order established.** P0 (auth, kite_session, symbol_mapper, orders, storage, watcher, main.py with DRY_RUN=true) → P1 (greeks, expiry_resolver, strike_selector, signal routing, premium sizing) → P2 (notifier, scheduler, logging, metrics, dashboard templates) → P3 (Docker, Terraform, CI). Full file tree and build-order table were proposed and are ready to execute once answers arrive.

3. **25 open questions are blocking code.** The critical blockers are: (a) **Python version** — machine has 3.14.4 but py_vollib C extensions may not support it; recommend Docker-pinned 3.11 or local pyenv 3.11; (b) **MIS vs NRML** — affects GTT OCO eligibility and square-off scheduler; (c) **capital per trade** — needed for risk.py and sizing tests; (d) **cloud host** — AWS Mumbai / DO Bangalore / Azure / GCP; (e) **Kite Connect app** — api_key and api_secret must exist before kite_session.py can be tested. Full question list is in the previous assistant message.

4. **No files created.** The repo contains only the four reference files (v1/v2/v3 specs + prototype) and the initial commit. `SESSION_HANDOFF.md` is the only new file from this session.

5. **Immediate next step (when you return).** Answer the 25 questions — especially the five critical blockers above. Once those are in, start coding in this order: `app/config.py` → `app/storage.py` → `app/auth.py` → `app/kite_session.py` → `app/symbol_mapper.py` → `app/orders.py` → `app/watcher.py` → `app/main.py` (DRY_RUN=true throughout P0).

---

## Proposed File Tree (reference)

```
tv-zerodha-bot/
├── app/
│   ├── main.py, auth.py, config.py, kite_session.py
│   ├── symbol_mapper.py, expiry_resolver.py
│   ├── greeks.py, strike_selector.py
│   ├── risk.py, orders.py, watcher.py
│   ├── storage.py, notifier.py, scheduler.py
│   └── templates/  (dashboard.html, positions.html, history.html)
├── tests/
├── infra/  (Terraform)
├── pinescript/
├── simulation_mode.py
├── Dockerfile, docker-compose.yml, Caddyfile
├── .env.example, requirements.txt, pyproject.toml
└── .github/workflows/ci.yml
```
