\--- BEGIN UPDATE ---

The v1 design places orders on the instrument that TradingView sent the signal on. I want to change that: every BUY signal should buy a call option (CE), every SELL signal should buy a put option (PE), with strike selected by delta ≈ 0.65, for all underlyings except Natural Gas. Keep everything else from v1 intact (1:2 R:R, GTT OCO exits, static IP, market protection, etc.) — this update only changes the instrument-resolution and strike-selection stages.



1\. Signal → Instrument translation (new)

When a validated alert arrives, route it based on the underlying:

Underlying (from TV ticker)BUY signal →SELL signal →NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX (indices)Buy CE, delta ≈ 0.65Buy PE, delta ≈ 0.65Any NSE equity F\&O stock (RELIANCE, HDFCBANK, etc.)Buy stock CE, delta ≈ 0.65Buy stock PE, delta ≈ 0.65MCX CRUDEOIL, GOLD, SILVER, COPPER, etc.Buy CE, delta ≈ 0.65Buy PE, delta ≈ 0.65MCX NATURALGASTrade the near-month NG future directly (BUY → long future, SELL → short future), 1:2 R:R from v1 applies unchangedsameAnything else not in the F\&O universeReject with Telegram alert: "No options available for {symbol}"same

Rationale for the NG exception: Natural Gas options on MCX are thin and wide-spread, so delta-based strike selection is unreliable. The future is the honest instrument.

The NG detection must use the underlying name from the Kite instruments master (name == "NATURALGAS"), not a string match on the TV ticker — TradingView may send MCX:NATURALGAS1!, NATURALGAS26MARFUT, or similar variants.

Important semantic note: we are always BUYING options (long premium) on both BUY and SELL signals — a "SELL" TV signal does NOT mean shorting/writing a PE; it means buying a PE to profit from downside. Document this clearly in the README so no one ever short-sells premium by accident.



2\. Expiry selection (new)

Add a config block OPTION\_EXPIRY\_RULE with these modes (default = NEAREST\_WEEKLY):



NEAREST\_WEEKLY — for indices that have weekly expiries (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX; the available weekly set changes periodically per SEBI circulars, so derive it from the instruments dump, don't hardcode).

NEAREST\_MONTHLY — for stock options and commodities (which do not have weeklies).

SKIP\_EXPIRY\_DAY — if the nearest expiry is today and time is past a configurable cutoff (default 14:30 IST), roll to the next expiry. Rationale: theta is vicious on expiry day and a 1:2 R:R breakeven is unlikely.

MIN\_DAYS\_TO\_EXPIRY — reject any trade whose selected expiry is fewer than N days away (default 1 for indices, 3 for stocks/commodities).



Derive the expiry set per underlying from the instruments master filtered by name == underlying and instrument\_type in ("CE","PE"); take the distinct expiry column, sort, and pick per the rule above.



3\. Delta computation (new module: app/greeks.py)

Kite Connect does not publish Greeks. Compute delta locally using Black-Scholes.

Library: py\_vollib (or py\_vollib\_vectorized for speed on large chains). Install py\_vollib py\_lets\_be\_rational. Fallback: mibian.

Inputs needed per strike:



S = spot price of underlying (fetch via kite.ltp("NSE:NIFTY 50") etc.; for stock options use the equity LTP; for MCX commodities use the near-month future's LTP as the spot proxy since commodity options are options-on-future).

K = strike price (from instruments master).

t = time to expiry in years, computed as seconds-to-expiry / (365 × 86400). Use the expiry datetime at 15:30 IST (equity/index) or 23:30 IST (MCX) — expose this per segment.

r = risk-free rate. Default to 0.065 (6.5%, current India 10-yr G-sec proxy). Make it a config value, not a literal.

sigma = implied volatility. This is the hard part — see below.

flag = 'c' for CE, 'p' for PE.



IV back-solving: since Kite gives us option LTP but not IV, use py\_vollib.black\_scholes.implied\_volatility.implied\_volatility(price, S, K, t, r, flag) to back-solve IV from the mid-price of each strike, then feed that IV back into py\_vollib.black\_scholes.greeks.analytical.delta(flag, S, K, t, r, sigma). Yes this is circular-looking but it is the industry-standard approach.

Edge cases to handle:



Implied-vol solver fails (option deep OTM, zero bid, stale quote) → skip that strike, don't crash.

Option LTP is 0 or None → skip.

Very-near-expiry (t < 1 hour) → delta from BS breaks down; if SKIP\_EXPIRY\_DAY didn't already filter it, refuse and alert.

For commodity options on MCX, use the Black-76 model (options on futures), not Black-Scholes — py\_vollib.black module. This matters: using BS on commodity options will mis-price delta by enough to pick the wrong strike.





4\. Strike selection algorithm (new)

def select\_strike(underlying, expiry, flag, target\_delta=0.65, tolerance=0.05):

&#x20;   # 1. Fetch all strikes for (underlying, expiry, flag) from instruments master.

&#x20;   # 2. Batch-fetch LTPs via kite.quote() — max 500 instruments per call;

&#x20;   #    chain size is usually 30-80 strikes so one call suffices.

&#x20;   # 3. For each strike, compute IV then delta. PE deltas are negative —

&#x20;   #    compare on abs(delta).

&#x20;   # 4. Filter to strikes with valid (non-NaN) delta.

&#x20;   # 5. Pick argmin(abs(abs(delta) - target\_delta)).

&#x20;   # 6. Tie-breaker (distances equal within 0.001): prefer the ITM strike

&#x20;   #    (higher abs-delta side). Rationale: directional conviction over theta.

&#x20;   # 7. Sanity guardrails, ALL must pass — else reject and Telegram-alert:

&#x20;   #      - |computed\_delta - target\_delta| <= tolerance (default 0.05)

&#x20;   #      - option LTP > MIN\_PREMIUM (default ₹2) — avoid illiquid junk

&#x20;   #      - bid-ask spread <= MAX\_SPREAD\_PCT of LTP (default 5%)

&#x20;   #      - open interest > MIN\_OI (default 1000 for indices, 100 for stocks)

&#x20;   # 8. Return the chosen instrument + computed delta + IV used, for logging.

Every selection decision (all candidates, their deltas, the winner, the rejection reasons) must be logged to the audit table so I can audit later why it picked what it picked.



5\. Position sizing for option buys (replaces v1 §6 step 5 for option trades)

For option buys, the premium paid is the maximum loss. So two sizing modes:



PREMIUM\_BASED (default): qty = floor(capital\_per\_trade / option\_ltp / lot\_size) \* lot\_size. Straightforward.

UNDERLYING\_RISK\_BASED: compute what the underlying SL would be (per v1 rules), translate to option-price move via delta \* underlying\_sl\_dist, size so option-leg loss matches risk\_per\_trade. More faithful to 1:2 R:R on the underlying but depends on delta holding during the move.



Default to PREMIUM\_BASED for v2 — it's robust and doesn't require live delta re-evaluation. Flag the other mode for later.

Apply a hard minimum premium floor (configurable, default ₹5 for index options, ₹2 for stock/commodity options): below that, reject as illiquid. Sub-₹2 options are the single biggest source of blowups for retail option buyers.



6\. Stop-loss and target for option buys (revises v1 §6)

For option buys, entry is the option premium, not the underlying price. Two interpretations of "1:2 R:R" are possible — pick one and stick to it:

Option A — R:R on the option premium itself (simpler, default):



sl\_price = entry\_premium \* (1 - SL\_PREMIUM\_PCT) (default 30% → SL if option loses 30%).

target\_price = entry\_premium + 2 \* (entry\_premium - sl\_price).

Easy to reason about, no delta-drift issues, but divorced from the underlying move.



Option B — R:R on the underlying, translated via delta:



Underlying SL-distance from v1 is preserved.

Option SL ≈ entry\_premium - delta \* underlying\_sl\_dist.

Option target ≈ entry\_premium + delta \* 2 \* underlying\_sl\_dist.

Faithful to the original thesis, but delta is dynamic — by the time target is hit, the effective R:R has drifted. Still acceptable as an approximation.



Default to Option A for v2 with SL\_PREMIUM\_PCT = 0.30. Expose Option B behind a config flag. Round both prices to the option's tick\_size (usually 0.05 for NSE options, 0.10 for some MCX).

GTT OCO placement on option legs is the same as v1 — two LIMIT orders at SL and target premium, trigger\_values set appropriately, transaction\_type=SELL since we bought the option.



7\. Pre-trade additional checks (add to v1 §6 step 6)

Before placing an option buy, also check:



Margin sufficient for the premium (option buying blocks full premium, no leverage).

Freeze quantity: for NIFTY/BANKNIFTY, exchange limits single-order qty. If qty > freeze\_qty, slice into multiple orders (the Kite API does this via variety=iceberg or manual slicing; use iceberg if the lot count warrants).

MCX options trading hours: commodities trade until 23:30 IST — adjust the intraday-square-off scheduler accordingly (v1's 15:15 square-off was NSE-centric).





8\. New modules / file changes

Add:



app/greeks.py — IV solver + delta function, BS-for-NSE and Black-76-for-MCX dispatcher.

app/strike\_selector.py — the select\_strike() logic from §4.

app/expiry\_resolver.py — expiry picker per §2.



Modify:



app/symbol\_mapper.py — new method resolve\_underlying(tv\_ticker) -> Underlying that returns a structured object {name, segment, is\_natural\_gas, spot\_source} rather than a single instrument. The option instrument is then resolved after strike selection.

app/orders.py — route through option-selection for non-NG, through v1 future logic for NG.

app/risk.py — add premium-based sizing mode.

app/config.py — new settings: TARGET\_DELTA=0.65, DELTA\_TOLERANCE=0.05, OPTION\_EXPIRY\_RULE, MIN\_DAYS\_TO\_EXPIRY\_INDEX, MIN\_DAYS\_TO\_EXPIRY\_STOCK, SL\_PREMIUM\_PCT, RR\_RATIO=2.0, RISK\_FREE\_RATE, MIN\_OPTION\_PREMIUM, MIN\_OI\_INDEX, MIN\_OI\_STOCK, MAX\_SPREAD\_PCT, SIZING\_MODE, NATURAL\_GAS\_NAMES=\["NATURALGAS","NATGASMINI"].

requirements.txt — add py\_vollib, py\_lets\_be\_rational, scipy.





9\. Additional tests



Unit test greeks.py against a known-good reference: for S=25000, K=25000, t=7/365, r=0.065, sigma=0.15, delta(CE) ≈ 0.52, delta(PE) ≈ -0.48. Allow 0.02 tolerance.

Unit test strike\_selector.py with a synthetic chain: verify it picks the 0.65-delta strike and respects the ITM tie-breaker.

Integration test: mock a full NIFTY CE chain (30 strikes), confirm the bot picks one strike, sizes correctly, places one entry, and sets up GTT OCO.

End-to-end dry-run against a Crude Oil weekly expiry (tests the Black-76 path).

Specific Natural Gas test: send a MCX:NATURALGAS1! BUY signal and confirm the bot routes to the near-month future, not to an option.





10\. Runbook additions



"Strike selection returned no valid candidate — what now?" → check IV solver logs, check if option chain was stale, check OI/spread guardrails.

"Bot picked a strike with delta 0.45, not 0.65" → tolerance band check, then IV data quality check.

"Natural Gas signal went to an option by mistake" → verify NATURAL\_GAS\_NAMES config matches the actual instrument name in the dump (Zerodha has used both NATURALGAS and NATGASMINI historically).





11\. What I'm still NOT asking for



Multi-leg option strategies (spreads, straddles, condors).

Delta-hedging or gamma-scalping.

IV-rank / IV-percentile filters — though this is a natural v3 addition.

Selling/writing options — v2 is long-premium only.





12\. Confirm before coding

On top of v1's questions, confirm:



TARGET\_DELTA = 0.65 and DELTA\_TOLERANCE = 0.05?

NEAREST\_WEEKLY for indices, NEAREST\_MONTHLY for stocks \& commodities — agree?

Default SL = 30% of premium (Option A), with Option B available via flag — agree?

On Natural Gas, trade the near-month future with v1's 1:2 R:R logic — agree?

Premium-based sizing as default — agree?

Use Black-76 for MCX commodity options, Black-Scholes for NSE index/stock options — agree?



Default to the safer / simpler choice on each if I don't respond, and call it out in the README.

\--- END UPDATE ---

