# Trade Desk (`/control`) — swipeable 4-tab dashboard

Single page, single request — all 4 tabs are rendered server-side in one response and swiped between client-side (CSS `scroll-snap`, no per-tab network round-trip). Built this way because the page is used almost entirely on an iPhone: a bottom tab bar (`.tabbar`, fixed) plus horizontal swipe between panels replaced the old single long-scrolling page. Shipped 2026-07-22 (commit 4d5819d), superseding the earlier two-column `.grid2` layout.

| Tab | `#panel` id | Contents |
|-----|------------|----------|
| Home | `home` | Today P&L hero + sparkline, risk-guard meters, Open Positions (live greeks), Straddle Defense, Quick Trade panel |
| Markets | `markets` | Volatility Monitor (ATM-IV chart), Commodity Intelligence |
| Settings | `settings` | Today's Schedule, Mode switches, Risk Controls (partial booking/entry wings), Kite Session, Background Jobs, Risk Parameters drawer |
| More | `more` | Link-tiles to the rarely-used pages: Orders, GTTs, History, Alerts, Agents, Desk |

- `_status_strip(settings)` renders the emergency-stop banner + annunciator pill strip; `_shell()` calls it for **every** page (not just /control), so estop/paper/mode/session status is visible everywhere.
- `_SWIPE_JS`: tab tap gives instant feedback (active class + `#swipe` height + `sessionStorage`/URL-hash persistence all update synchronously, not on scroll-settle) and also drives the native swipe; a `ResizeObserver` on each panel keeps `#swipe`'s height pinned to the active panel even as async content (live positions, commodity cards) changes its size after first paint — panels differ a lot in natural height (~280px More vs ~1950px Settings) and `align-items:flex-start` on the flex row stops them stretching to the tallest.
- **The tab bar's `<a href='/control#tab'>` markup must render *before* `.wrap`'s content in `_shell()`** — an inline script inside `content` runs as the parser reaches it and can't see DOM nodes that appear later in the HTML source. When tabbar rendered after content, `_SWIPE_JS` captured an empty node list and tab-highlighting silently no-opped while everything else worked (confusing partial failure). `position:fixed` makes the early placement visually harmless.
- Toggle/update POSTs under `/control/*` still redirect to plain `/control` (unchanged); the swiped-to tab survives that reload via `sessionStorage`, not a server-side redirect target.
- Orders and GTTs cross-link via a `.seg` control (`_book_seg`) instead of being separate tabs. Performance/scorecard/activity-feed content that used to live on /control now lives on `/history` (see `_performance_blocks`/`_scorecard_html`/`_activity_feed_html`, shared helpers next to `_shell`).

## Styling & JS infrastructure

- `_CSS` in app/main.py is a design-token system (CSS custom properties on `:root`, dark mode via `prefers-color-scheme`). Legacy class contracts (.card .ct .pill .btn .mr .mdr .rail .feed .sct …) are kept so /orders, /gtts, /history inherit the skin without markup changes.
- `_shell` = single frosted sticky topbar (`.topbar/.tb-*`), adaptive theme-color metas, `xl=True` kwarg → 1200px wrap.
- Charts (day sparkline, equity curve, Volatility Monitor `_VOL_MONITOR_JS`) read CSS variables at draw time and redraw on theme change. Vol Monitor pulls `/commodity-agents/{c}/iv-history?limit=500` (needs zb_session cookie — 401s when logged out).
- `/api/control/summary` 30s JSON poll patches meters/hero/kite-pill/next-job in place; page self-reloads when estop/paper/trade-mode change server-side. `PnlSnapshot` table + `pnl_snapshot_job` (5-min cron Mon–Fri 09–23 IST, prunes >60 days) feeds the Today-card intraday sparkline.
- Mobile-first: user drives this from an iPhone (390px). 16px inputs/selects (Safari auto-zoom), 44px touch targets under `(hover:none) and (pointer:coarse)`, `env(safe-area-inset-*)` padding. Verify zero horizontal overflow via `document.documentElement.scrollWidth`.

## Browser-pane testing quirk

`swipe.scrollTo({behavior:'smooth'})` doesn't reliably animate in the Claude Browser pane's automated rendering context (direct `scrollLeft=` assignment always works; second+ smooth calls sometimes never progress). Harness quirk, not a Safari issue — the instant-feedback design sidesteps it.
