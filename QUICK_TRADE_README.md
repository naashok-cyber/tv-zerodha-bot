# Quick Trade Panel — /control Dashboard

## What was added

A "Quick Trade" card at the top of `https://naashshla.duckdns.org/control` that lets you place voice-style trades directly from any browser — no iOS Shortcut needed.

**Files changed:**
- `app/main.py` — added `_QUICK_TRADE_PANEL` constant (HTML + vanilla JS) and prepended it to the `/control` page body. No other backend changes.

**No new backend endpoints.** All API calls go to the existing voice endpoints:
- `POST /voice/transcribe` — parse trade text → pending order
- `POST /voice/confirm` — execute the pending order
- `POST /voice/cancel` — discard the pending order
- `GET /admin/voice/status` — optional channel status check

---

## How to use it

1. Open `https://naashshla.duckdns.org/control` and log in
2. Open **Settings ⚙️** at the bottom of the Quick Trade card
3. Paste your `VOICE_AUTH_TOKEN` and tap **Save** (stored in `localStorage` on this device only)
4. Type or dictate a trade in the textarea — e.g. *"buy one lot NIFTY ATM call"*
5. Tap **Parse Trade** — Claude NLU parses it and shows a confirmation card
6. Review the summary, confidence score, and any warnings
7. Tap **Execute Trade** to send the order (or **Cancel** to discard)
8. The result appears inline; UI resets after 3 seconds ready for next trade

The **Session Log** below the panel shows your last 10 Quick Trades for the current browser session.

---

## Auth token

Your `VOICE_AUTH_TOKEN` is the same token in `/opt/tv-zerodha-bot/.env` on the server:

```bash
grep '^VOICE_AUTH_TOKEN=' /opt/tv-zerodha-bot/.env
```

**The token is stored only in your browser's `localStorage`** under the key `voiceAuthToken`. It is never logged, never sent anywhere except your own HTTPS endpoint, and never committed to git. Use **Clear Token** in Settings before sharing your browser or device.

Optionally store your `ADMIN_AUTH_TOKEN` (key `adminAuthToken`) to see the voice channel enabled/disabled status in the Settings panel.

---

## Behavior notes

- **Low confidence** (< 85%): yellow warning shown; you must explicitly confirm
- **EXIT/SQUARE-OFF**: red warning shown; double-confirm required (two separate Execute taps as the server enforces a two-step flow)
- **Token expiry**: 60s countdown timer; Execute button disables at 0; Re-parse button appears
- **Page auto-refresh**: the `/control` page normally refreshes every 30s. The panel suppresses this while a confirmation is pending, and restores it after execution, cancel, or expiry — so the page data stays fresh but mid-trade state is never lost
- **Paper mode**: if `DRY_RUN=true` on the server, Execute returns a paper trade result — correct behavior, no override needed

---

## Relationship to iOS Shortcut

The iOS Shortcut for voice trading remains available as an alternative client but is no longer the primary path. **The Quick Trade panel in `/control` is now the recommended manual trade interface.** The Shortcut and the panel both call the same backend endpoints with the same token — they can coexist.

---

## Rollback

The change is a single string constant (`_QUICK_TRADE_PANEL`) prepended to the `/control` body. To revert:

```bash
git revert HEAD   # reverts just this commit
git push origin master
# then on server: git pull && docker compose up -d --build bot
```
