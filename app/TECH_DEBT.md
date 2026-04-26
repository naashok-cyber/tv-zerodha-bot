# Tech Debt Register

Items to address before production deploy or P3 polishing. Owner: track in SESSION_HANDOFF.md.

---

## Open items

### TD-1: _QUOTE_CHUNK chunking path untested at >500 strikes
**File:** `app/strike_selector.py` — `_QUOTE_CHUNK = 500`  
**Risk:** The chunking logic exists but has never been exercised. A NIFTY chain rarely exceeds 500 strikes, but BANKNIFTY at narrow intervals could. An off-by-one or merge bug in `_quote_all()` would silently drop instruments.  
**Fix:** Add a fixture with 600+ synthetic CE instruments and assert all instruments are quoted and considered. Target: P2 polishing sprint.

---

### TD-2: ROUND_HALF_UP tick rounding — verify against NSE rejections
**File:** `app/symbol_mapper.py` — `round_to_tick()`  
**Risk:** ROUND_HALF_UP is a reasonable default but NSE/MCX may reject orders where the rounded price falls on a tick boundary edge case. Need at least one live production smoke test.  
**Fix:** Log the pre- and post-round price on every order placement; compare against exchange acknowledgement in the first live session. Revisit if rejections appear.

---

### TD-3: Legacy `SKIP_EXPIRY_DAY_CUTOFF_HOUR/MINUTE` config constants
**File:** `app/config.py`  
**Risk:** These two `int` fields are superseded by `SKIP_EXPIRY_CUTOFF_NSE = "14:30"` / `SKIP_EXPIRY_CUTOFF_MCX = "22:00"` added in P1-b. The old fields are not read by any P1+ code but still appear in `Settings`, can be set via env var, and may confuse operators who assume they control the cutoff.  
**Fix:** Remove `SKIP_EXPIRY_DAY_CUTOFF_HOUR` and `SKIP_EXPIRY_DAY_CUTOFF_MINUTE` from `Settings` in the P3 cleanup pass before production deploy. Update `.env.example` accordingly.

---

### TD-4: `OrderWatcher.start()` not wired into token-receive callback
**File:** `app/kite_session.py` — `handle_callback()`; `app/main.py` — lifespan  
**Risk:** `OrderWatcher` is created at startup (P0-e) but never started because no Kite token exists at cold boot. The natural hook is `handle_callback()` — the moment the daily token arrives. Until this is wired, GTT fills are not tracked and breakeven/trail logic cannot fire.  
**Fix:** Inside `handle_callback()`, after persisting the token, call `watcher.start()`. Target: P1-c (first task, before pipeline wiring).
