\--- BEGIN PROMPT ---

You are a senior Python/DevOps engineer. Build me a production-grade, SEBI-compliant, cloud-hosted bot that receives TradingView webhook alerts and places corresponding orders on Zerodha via the Kite Connect API, with automated stop-loss and target using a 1:2 risk-to-reward ratio.

Treat this as a complete delivery: architecture, code, infra-as-code (or clear manual steps), compliance checklist, test plan, and runbook. Ask me to clarify anything in the \[CHOOSE: ...] blocks that I have not filled in. Do not assume.



1\. My configuration (fill these in)



Segment(s) I trade: \[CHOOSE: NSE equity cash / NFO index futures / NFO stock futures / NFO options / MCX]

Product type: \[CHOOSE: MIS (intraday) / CNC (delivery equity) / NRML (overnight F\&O)]

Capital per trade (₹): \[CHOOSE: fixed amount, e.g. 10000] OR risk per trade: \[CHOOSE: % of capital, e.g. 1%]

Position sizing mode: \[CHOOSE: fixed quantity / fixed rupee notional / risk-based (quantity = risk\_amount / (entry - SL))]

R:R ratio: 1:2 (target distance = 2 × stop-loss distance). Do not hard-code; expose as a config so I can change it later.

SL source: \[CHOOSE: computed from ATR in the webhook payload / fixed % of entry / explicit SL price sent from TradingView]

Cloud host: \[CHOOSE: AWS EC2 (ap-south-1 Mumbai) / DigitalOcean Bangalore / Azure India / GCP asia-south1] — must support a reserved/Elastic static IPv4 (SEBI mandate effective 1 April 2026).

My primary static IP is already acquired / needs to be provisioned: \[CHOOSE]

Runtime: Python 3.11+, kiteconnect official SDK, FastAPI (preferred) or Flask.

Secrets store: \[CHOOSE: AWS Secrets Manager / HashiCorp Vault / .env file encrypted / Doppler]

Notifications: \[CHOOSE: Telegram bot / email (SES/SMTP) / both] for order fills, rejects, and errors.





2\. Regulatory \& broker constraints you MUST respect

These are not optional. Code must enforce or document each one.



SEBI retail algo framework (Feb 2025 circular, enforced 1 April 2026): every API order must originate from a static IP registered on the Kite Connect developer dashboard (developers.kite.trade → profile → IP Whitelist). Orders from unregistered IPs are rejected.

Orders-per-second cap: Kite API enforces 10 OPS. Above this, strategy registration with exchange + Algo-ID is required. Design the bot for ≤ 10 OPS and back off on HTTP 429.

Manual daily login is mandated by the exchange. The access\_token expires daily around 06:00 AM IST. Fully automating login via Selenium/TOTP is discouraged by Zerodha and not officially supported. Build a semi-automated daily login flow:



At \~07:30 IST every trading day, bot sends me a Telegram message with the Kite login URL.

I complete login + TOTP manually; Zerodha redirects to my configured redirect URL carrying request\_token.

A callback endpoint exchanges request\_token for access\_token using api\_secret and stores it encrypted.

Bot refuses to place orders if access\_token is missing or older than \~20 hours.

Also implement the pyotp/TOTP auto-login path as a fallback behind a feature flag, clearly labeled "unofficial, use at own risk," for people who accept that trade-off.





Market protection is mandatory on all market orders (including SL-M) from 1 April 2026. Use market\_protection=-1 (or a numeric percent I can configure). Reject the order locally if the parameter is missing.

Bracket Orders (BO) are discontinued. squareoff and stoploss params in place\_order are deprecated. Use GTT OCO (One-Cancels-Other) for simultaneous SL + target after entry fills. For intraday MIS where GTT OCO is not allowed for the product, fall back to two separate limit/SL-M orders and manage OCO logic in the bot (cancel sibling on fill via websocket order updates).

Hosting location: SEBI requires retail algos to be hosted on Indian servers. Pick the India region explicitly.

Audit trail: log every inbound webhook and every outbound broker call for 5 years (immutable, append-only, e.g. S3 with object lock or equivalent).

2FA, OAuth, password expiry, encrypted transport must all be in place — they already are on Kite's side; don't regress them.





3\. High-level architecture

Produce an architecture diagram (ASCII is fine) and implement:

TradingView Alert ──HTTPS POST──▶ Reverse Proxy (Caddy/nginx, TLS via Let's Encrypt)

&#x20;                                       │

&#x20;                                       ▼

&#x20;                             FastAPI webhook receiver

&#x20;                                       │

&#x20;                       ┌───────────────┼──────────────────┐

&#x20;                       ▼               ▼                  ▼

&#x20;               IP + HMAC auth    Idempotency check   Payload validator

&#x20;                       │               │                  │

&#x20;                       └───────────────┴──────────────────┘

&#x20;                                       │

&#x20;                                       ▼

&#x20;                             Symbol Mapper (TV → Kite)

&#x20;                                       │

&#x20;                                       ▼

&#x20;                            Risk Manager + Position Sizer

&#x20;                                       │

&#x20;                                       ▼

&#x20;                          Kite Order Executor (kiteconnect)

&#x20;                        │                              │

&#x20;                        ▼                              ▼

&#x20;                Entry order placed             WebSocket order updates

&#x20;                        │                              │

&#x20;                        └───────┬──────────────────────┘

&#x20;                                ▼

&#x20;                  On fill → place GTT OCO (SL + Target, 1:2)

&#x20;                                │

&#x20;                                ▼

&#x20;               Telegram notifier + Postgres/SQLite audit log

Key services / modules (create each as its own file):



app/main.py — FastAPI app, /webhook (POST) and /kite/callback (GET for login redirect).

app/auth.py — IP allowlist, HMAC/shared-secret verification, rate limiting.

app/kite\_session.py — loads/refreshes access\_token, exposes a singleton KiteConnect client.

app/symbol\_mapper.py — see Section 5.

app/risk.py — position sizing, 1:2 R:R computation, daily loss limit, max open positions.

app/orders.py — place\_entry(), place\_gtt\_oco(), cancel\_all(), tick-size rounding, market-protection injection.

app/watcher.py — KiteTicker websocket subscription that listens for order\_update postbacks; on COMPLETE status of entry, triggers GTT OCO placement.

app/storage.py — Postgres or SQLite with SQLAlchemy; tables: alerts, orders, gtts, errors, sessions.

app/notifier.py — Telegram + email.

app/config.py — pydantic-settings, loads from env / secrets manager.

app/scheduler.py — APScheduler: daily 07:30 IST login reminder, 08:30 IST instruments.csv refresh, 15:20 IST intraday square-off for MIS, end-of-day audit log flush.

tests/ — pytest, including a Kite sandbox/mock mode that never hits the real API.





4\. TradingView alert contract

Pine Script alert message MUST be valid JSON. Use this exact schema and have TradingView's placeholder substitution fill values. Generate a helper Pine Script library that produces this JSON in alert() calls so I don't hand-write it.

json{

&#x20; "secret": "<HMAC\_SHARED\_SECRET>",

&#x20; "strategy\_id": "ema\_crossover\_v1",

&#x20; "tv\_ticker": "{{ticker}}",

&#x20; "tv\_exchange": "{{exchange}}",

&#x20; "action": "BUY",                      // or "SELL" / "EXIT"

&#x20; "order\_type": "MARKET",               // or "LIMIT" / "SL" / "SL-M"

&#x20; "entry\_price": {{close}},             // used for LIMIT or as reference

&#x20; "stop\_loss": null,                    // explicit SL price (optional)

&#x20; "sl\_percent": 0.5,                    // OR percent-based SL

&#x20; "atr": null,                          // OR ATR value for SL distance

&#x20; "quantity\_hint": null,                // optional override

&#x20; "product": "MIS",

&#x20; "time": "{{timenow}}",

&#x20; "bar\_time": "{{time}}",

&#x20; "interval": "{{interval}}"

}

Webhook handler rules:



Reject any payload where secret does not match the configured HMAC (use constant-time compare).

IP allowlist the four published TradingView webhook IPs: 52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7. TradingView only supports ports 80/443 and has a 3-second timeout — respond fast (202 Accepted) and process async in a background task.

Idempotency: hash (strategy\_id + tv\_ticker + bar\_time + action) as the idempotency key; dedupe on repeated deliveries.

Validation: pydantic model with strict types; reject unknown fields.





5\. Symbol mapping: TradingView ↔ Zerodha Kite (CRITICAL)

TradingView tickers and Kite tradingsymbol values DO NOT always match. Build symbol\_mapper.py that:



Downloads the full instruments dump daily at 08:30 IST from https://api.kite.trade/instruments (CSV, \~40 MB) into a local SQLite table. Fields: instrument\_token, exchange\_token, tradingsymbol, name, expiry, strike, tick\_size, lot\_size, instrument\_type, segment, exchange.

Exposes resolve(tv\_ticker, tv\_exchange, hint=None) -> KiteInstrument covering these cases:



TradingView inputKite tradingsymbol + exchangeNotesNSE:RELIANCERELIANCE on NSEstrip prefixBSE:SENSEXindex, not orderablereject or map to SENSEX futureNSE:NIFTY (index)Not orderable. Map to current-month NIFTY<YY><MON>FUT on NFO or user-specified strikeneeds hint.instrument: FUT or OPT + strike + CE/PENSE:BANKNIFTYsame rule for BANKNIFTYweekly and monthlyFutures monthly: NIFTY26JANFUT on NFOtradingsymbol = NIFTY26JANFUTformat: SYMBOL + YY + MMM + FUTOptions weekly: NIFTY2611323500CENFO weekly optionformat: SYMBOL + YY + M + DD + STRIKE + CE/PE where M is 1–9 for Jan–Sep, O/N/D for Oct/Nov/DecOptions monthly: NIFTY26JAN23500CENFO monthly optionformat: SYMBOL + YY + MMM + STRIKE + CE/PEMCX futures: MCX:CRUDEOIL1!CRUDEOIL<YY><MON>FUT on MCXresolve continuous contract to near-month



Handles index aliasing for the quote API: NIFTY index quote is under key NSE:NIFTY 50, BANKNIFTY under NSE:NIFTY BANK, FINNIFTY under NSE:NIFTY FIN SERVICE. Keep a hard-coded dictionary for these.

Tick-size rounding: every order price must be a multiple of tick\_size. Implement round\_to\_tick(price, tick\_size) using Decimal, not float.

Lot-size enforcement: for F\&O, quantity must be a multiple of lot\_size. Reject or round down.

Expiry awareness: when TradingView sends a continuous futures ticker (e.g. NIFTY1!), resolve to the current near-month / nearest-weekly expiry automatically, with a configurable rollover N days before expiry.

Fallback: if resolution fails, do NOT guess. Log error, notify via Telegram, return HTTP 422 to TradingView.





6\. Order execution and 1:2 R:R logic

For each validated, mapped alert:



Compute stop-loss distance (sl\_dist):



If stop\_loss provided: sl\_dist = abs(entry - stop\_loss).

Else if atr provided: sl\_dist = atr\_multiplier \* atr (multiplier configurable, default 1.5).

Else if sl\_percent provided: sl\_dist = entry \* sl\_percent / 100.

Reject if none available.





Compute target distance: tgt\_dist = 2 \* sl\_dist (the 1:2 R:R).

For BUY: sl\_price = entry - sl\_dist, target\_price = entry + tgt\_dist. For SELL: reversed.

Round all three to instrument tick size.

Position size per chosen mode (see Section 1). Risk-based: qty = floor(risk\_amount / sl\_dist / lot\_size) \* lot\_size.

Pre-trade checks: sufficient margin (kite.margins()), within daily max trades, within daily max loss, market is open, instrument is not in a circuit, OPS rate limit.

Place entry:



MARKET → kite.place\_order(variety="regular", ..., order\_type="MARKET", product=<cfg>, market\_protection=-1)

LIMIT → pass price

SL / SL-M → pass trigger\_price (+ price for SL)





Subscribe to order updates via KiteTicker postbacks. When entry status becomes COMPLETE:



Record average fill price as the true entry; recompute SL and target from the actual fill (not the expected entry).

Place GTT OCO exit:







python     order\_dict = \[

&#x20;        {"exchange": ex, "tradingsymbol": ts, "transaction\_type": opposite,

&#x20;         "quantity": qty, "order\_type": "LIMIT",

&#x20;         "product": product, "price": sl\_limit\_price},   # leg 1 = stoploss

&#x20;        {"exchange": ex, "tradingsymbol": ts, "transaction\_type": opposite,

&#x20;         "quantity": qty, "order\_type": "LIMIT",

&#x20;         "product": product, "price": target\_limit\_price} # leg 2 = target

&#x20;    ]

&#x20;    kite.place\_gtt(

&#x20;        trigger\_type=kite.GTT\_TYPE\_OCO,

&#x20;        tradingsymbol=ts, exchange=ex,

&#x20;        trigger\_values=\[sl\_trigger, target\_trigger],

&#x20;        last\_price=ltp,

&#x20;        orders=order\_dict)



last\_price is mandatory and determines direction — fetch via kite.ltp().

For products where GTT OCO is unavailable (e.g. MIS on some segments), place two separate orders and run in-process OCO: websocket listener cancels the sibling when one fills.





EXIT alert from TradingView → cancel any open GTTs on that symbol, square off the net position with a MARKET order.

Intraday auto-square-off (MIS only): APScheduler job at 15:15 IST closes anything still open, 15:20 IST cancels any lingering GTTs.

Every broker call wrapped with exponential backoff on NetworkException and 429; never retry on InputException / TokenException — those are permanent and must alert me.





7\. Cloud deployment (SEBI static-IP compliant)



Provision in an Indian region (pick from Section 1). Output Terraform in infra/ (preferred) or clear manual steps.

Reserve a static public IPv4 (AWS Elastic IP / DO Reserved IP / Azure Public IP Standard SKU). All outbound API calls to api.kite.trade must egress from this IP — verify with a startup self-test that hits https://ifconfig.me and logs the IP.

Security group / firewall:



Inbound 443 open only to the four TradingView IPs listed above.

Inbound 22 open only to my management IP.

Outbound 443 to api.kite.trade, kite.zerodha.com, Telegram, Let's Encrypt.





TLS on the webhook endpoint (Caddy auto-HTTPS is simplest). TradingView requires HTTPS on 443 (port 80 also accepted but redirect to 443).

Systemd unit or Docker Compose with restart=always. Single instance, no autoscaling (orders must be deterministic).

Logs → CloudWatch / Loki; metrics (order\_placed\_total, webhook\_received\_total, api\_errors\_total, latency histograms) → Prometheus + Grafana or a hosted equivalent.

Backups: nightly dump of sessions, orders, audit tables to object storage with object-lock / immutability for 5 years.

Register the static IP on Kite Connect dashboard: https://developers.kite.trade → profile → IP Whitelist. Document this step in the runbook.

VPS alternative (cheaper): DigitalOcean Bangalore droplet with reserved IP (\~₹500–₹1,500/mo) vs AWS Mumbai EC2 t3.small + EIP (\~₹1,500–₹2,500/mo). Include cost table.





8\. Testing plan

Deliver these test levels:



Unit tests: symbol mapper (20+ cases covering equity/FUT/weekly-OPT/monthly-OPT/MCX/indices), tick-size rounding, R:R computation, lot-size enforcement, idempotency hash.

Integration tests against a mock Kite: use responses library to stub api.kite.trade. Verify: order payload shape, market-protection present, GTT OCO has correct leg ordering, tick/lot compliance.

Paper-trading mode: config flag DRY\_RUN=true that logs what would be sent without calling place\_order. Default the bot to DRY\_RUN on first install.

End-to-end smoke test: run against Kite's live API with the smallest possible real order (1 share of a liquid low-price stock) off a TradingView test alert. Document the exact checklist.

Load test the webhook: ensure it can absorb 50 alerts/minute bursts without dropping any (buffer via background task queue).

Failure drills: kill the process mid-entry, kill it after entry but before GTT placement, simulate websocket disconnect, simulate 429 storm, simulate expired token mid-day.





9\. Security



Never log secrets or full access tokens (redact).

Rotate HMAC shared secret quarterly.

Store api\_secret and access\_token encrypted at rest (KMS or libsodium).

SSH key auth only; disable password auth.

Fail2ban on the management port.

Dependency scanning (pip-audit) in CI.

Isolate the bot user (non-root, no shell for service account).





10\. Deliverables checklist

Produce everything below. Do not skip any item — if something is out of scope, say so and explain why.



&#x20;README.md with architecture, setup, compliance notes, runbook.

&#x20;Full source tree per Section 3.

&#x20;Dockerfile + docker-compose.yml.

&#x20;infra/ Terraform for the chosen cloud (or manual-steps.md).

&#x20;Pine Script helper library that emits the JSON in Section 4.

&#x20;Example TradingView alert-message templates for: equity BUY, equity SELL, NIFTY weekly CE entry, intraday EXIT.

&#x20;requirements.txt pinned.

&#x20;GitHub Actions CI (lint + pytest).

&#x20;.env.example with every variable documented.

&#x20;Compliance checklist mapping each SEBI/NSE requirement to the code path that satisfies it.

&#x20;Runbook covering: daily login, token-refresh failure, "orders being rejected — now what?", migrating to a new static IP, what to do if 429s appear, disaster recovery.

&#x20;Cost sheet for the chosen cloud provider.





11\. What I am explicitly NOT asking for (out of scope)



Strategy generation / backtesting — that lives in TradingView on my side.

Options Greeks, multi-leg strategies (iron condor etc.) — single-leg entries only for v1.

Mobile app.

Multi-broker support — Zerodha only for now, but keep the BrokerAdapter interface clean for later Upstox/Dhan/Fyers swaps.





12\. Things to ask me before you start coding

Before writing any code, confirm each of these with me:



All \[CHOOSE: ...] values above.

Which static IP I have (or whether you should allocate one via Terraform).

My Kite Connect api\_key storage location (don't paste it; just the path/secret name).

Whether the pyotp fallback auto-login should be enabled despite Zerodha's "not recommended" stance.

Telegram bot token status (already created? need instructions?).

Whether I want GTT OCO for MIS intraday (product-dependent — GTT was originally built for CNC; verify segment support at implementation time).



If I don't respond to these, default to the safer choice and call it out in the README.

\--- END PROMPT ---

