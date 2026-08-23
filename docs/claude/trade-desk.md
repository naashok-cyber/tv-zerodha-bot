# Trade Desk (`/control`) — swipeable 4-tab dashboard

Single page, single request — all 4 tabs are rendered server-side in one response and swiped between client-side (CSS `scroll-snap`, no per-tab network round-trip). Built this way because the page is used almost entirely on an iPhone: a bottom tab bar (`.tabbar`, fixed) plus horizontal swipe between panels replaced the old single long-scrolling page. Shipped 2026-07-22 (commit 4d5819d), superseding the earlier two-column `.grid2` layout.

| Tab | `#panel` id | Contents |
|-----|------------|----------|
| Home | `home` | Today P&L hero + sparkline, risk-guard meters, Open Positions (live greeks), Straddle Defense, Quick Trade panel |
| Markets | `markets` | Volatility & Intelligence — one ATM-IV tile per tracked scrip, agent verdict folded in |
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
- Charts (day sparkline, equity curve, volatility grid `_VOL_GRID_JS`) read CSS variables at draw time and redraw on theme change. The grid pulls `/commodity-agents/{c}/iv-history` per scrip plus `/recommendations` once (both need the zb_session cookie — 401 when logged out).

## Markets tab — volatility grid (`_VOL_GRID_JS`)

Small multiples + detail-on-demand, replacing the old single chart with its scrip dropdown. Every entry in `COMMODITY_AGENTS_COMMODITIES` gets a tile; the skeletons are server-rendered (`_VOL_LABELS` supplies display names, unlabelled scrips fall back to the raw key) and JS only fills `[data-r=…]` hooks, so the panel has its final shape before any fetch lands.

- **Collapsed** = bare trend-coloured sparkline, no axes/grid/date labels — at the ~150px a two-up phone grid allows, chrome would eat the line. Header shows the verdict pill; the trend pill shows the 24h delta alone (colour already carries direction, and pairing an arrow with a signed number reads as a double negative — note the two can legitimately disagree, e.g. a green `−0.0 pts` when the EMA turns up on a flat day).
- **Expanded** (`.vgt.on`, `grid-column:1/-1`) = the full axed chart with drag-zoom and tooltip, the stats readout, the agent's reasoning, and **Approve / Reject**. Accordion — only one open at a time, persisted in `localStorage['vg-open']`. Tapping a collapsed tile expands it; once expanded only `.vgt-h` collapses it, so dragging the chart or reaching for Approve can't fold it away.
- **Commodity Intelligence was merged in, not moved.** It showed ATM IV / IV trend / VRP for the same six scrips the chart already plotted, and re-fetched `iv-history?limit=40` per scrip to do it. Full agent detail still lives at `/commodity-agents/dashboard` (linked from More).
- Action buttons only ever render inside `.vgt-x`, which is `display:none` while collapsed — a money button is never one stray tap from a sparkline. The POST still needs a second confirming tap (`confirm_token`).
- `limitFor(rangeDays)` scales the fetch to the range (300/800/1600/2000) instead of always pulling 2000 — six tiles × 2000 rows on a phone was the alternative. The cache is keyed by scrip **and** limit, so widening the range refetches.
- **Redraw after expand is synchronous, not `requestAnimationFrame`.** The `.on` class is already applied to layout, so reading `clientWidth/Height` yields the new box; a rAF callback never fires at all if the page isn't compositing, which leaves the expanded tile stretching its 62px sparkline bitmap over 190px. rAF stays only in the coalescing `redraw()` used by the ResizeObserver and theme-change paths.
- `touch-action:none` is set on a canvas **only while expanded** — otherwise a horizontal drag starting over a sparkline gets swallowed instead of swiping the tab carousel.
- Phone grid is pinned to `minmax(0,1fr) minmax(0,1fr)` under 420px, not `auto-fit`: at 375px the track only just fits, and a scrollbar or a wider safe-area inset silently drops it to one column. `minmax(0,…)` rather than a bare `1fr` because `1fr` floors at min-content, which lets a long badge like `BUY · APPROVED` widen its column and skew the pair.
- `/api/control/summary` 30s JSON poll patches meters/hero/kite-pill/next-job in place; page self-reloads when estop/paper/trade-mode change server-side. `PnlSnapshot` table + `pnl_snapshot_job` (5-min cron Mon–Fri 09–23 IST, prunes >60 days) feeds the Today-card intraday sparkline.
- Mobile-first: user drives this from an iPhone (390px). 16px inputs/selects (Safari auto-zoom), 44px touch targets under `(hover:none) and (pointer:coarse)`, `env(safe-area-inset-*)` padding. Verify zero horizontal overflow via `document.documentElement.scrollWidth`.

## Browser-pane testing quirk

`swipe.scrollTo({behavior:'smooth'})` doesn't reliably animate in the Claude Browser pane's automated rendering context (direct `scrollLeft=` assignment always works; second+ smooth calls sometimes never progress). Harness quirk, not a Safari issue — the instant-feedback design sidesteps it.
