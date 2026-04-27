\# Project State — for new Claude.ai chat handoff



\## What this is

A cloud-hosted bot that receives TradingView webhook alerts and

places Zerodha Kite Connect orders. Long signals buy CE options

at delta \~0.65, short signals buy PE options at the same delta,

with 1:2 R:R on option premium. Natural Gas signals route to the

near-month future instead. SEBI-compliant (static IP, market

protection, daily login). Python 3.11 / FastAPI / SQLAlchemy /

KiteConnect.



\## Spec files in this folder (read these in order)

\- tradingview\_zerodha\_bot\_prompt.md (v1, base spec)

\- tradingview\_zerodha\_bot\_prompt\_v2\_options.md (v2, delta-0.65 logic)

\- tradingview\_zerodha\_bot\_prompt\_v3\_gap\_analysis.md (v3, prototype gap fixes)

\- SESSION\_HANDOFF.md (most recent operational state, updated by Claude Code)



\## Where the build stands (as of resume point)

\- 18 commits on master, 199 tests passing

\- P0 complete (auth, kite\_session, symbol\_mapper, orders, watcher, main)

\- P1-a complete (greeks: BSM for NSE, Black-76 for MCX)

\- P1-b complete (expiry\_resolver + strike\_selector with audit log)

\- P1 pre-flight done (scipy, py\_vollib, py\_lets\_be\_rational installed)

\- Pine Script alert emitter shipped

\- TECH\_DEBT.md exists with deferred items



\## Next phase: P1-c

Signal routing wired into main.py background task:

\- BUY -> buy CE at delta 0.65

\- SELL -> buy PE at delta 0.65

\- NATURALGAS -> trade near-month future, 1:2 R:R on future price

\- All other underlyings use option pipeline

\- Place entry, on COMPLETE postback place GTT OCO

&#x20; (SL = 30% of premium, target = 1:2 R:R)

\- DRY\_RUN=true must skip all broker calls



After P1-c: P1-d (risk.py — premium sizing + daily caps +

consecutive losses kill switch).



\## Key decisions already made (do not re-ask)

\- Segment: NFO index options (NIFTY, BANKNIFTY) + MCX commodity options

\- Product: NRML only (no MIS)

\- Capital per trade: ₹10,000; total capital ₹1,00,000; 1% risk cap

\- Cloud: AWS Mumbai (Terraform generate only, manual apply later)

\- Trail mode: bar-close only via TradingView "TRAIL" webhook

\- Database: SQLite for now

\- Notifications: Telegram only (bot creation deferred to P2)

\- pyotp auto-login: disabled

\- Underlyings at launch: NIFTY weekly first, then BANKNIFTY

\- NATURALGAS: near-month future path

\- TARGET\_DELTA = 0.65, DELTA\_TOLERANCE = 0.05

\- SL Mode: Option A (30% of premium, 1:2 R:R on premium itself)

\- Sizing: PREMIUM\_BASED (capital / option\_ltp / lot\_size)

\- Greeks: BSM for NSE, Black-76 for MCX (commodity options)

\- Breakeven 1.0R, trail trigger 1.5R, trail distance 0.5R, on PREMIUM

\- MAX\_DAILY\_LOSS = ₹2,000 absolute, also 2% of TOTAL\_CAPITAL secondary cap

\- Consecutive losses kill switch = 3

\- Risk-free rate 6.5%, NSE dividend yield 0.0

\- PBKDF2\_ITERATIONS = 600000



\## Build discipline rules

1\. Always commit after each phase before starting the next

2\. All Kite calls mocked in tests; no live network

3\. Type hints everywhere; Decimal for prices crossing module boundaries

4\. IST timezone via zoneinfo, take `now` as injected parameter

5\. Stop after pytest is green; show test count + decisions beyond spec

6\. Phase-gate strictly — one P-phase per session approval round



\## Billing setup

\- Claude Code is on API key billing (prepaid balance, hard cap)

\- Token discipline: /compact between phases, /cost to monitor



\## Resume command for new chat

Paste the prompt below into the new chat to brief it.

