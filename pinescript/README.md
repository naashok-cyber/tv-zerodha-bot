# Pine Script: Alert Emitter Library & EMA Strategy

## 1. Publish the library to TradingView

1. Open the Pine Script editor in TradingView.
2. Paste the contents of `alert_emitter.pine`.
3. Click **Save As Library** — name it `AlertEmitter`.
4. Click **Publish script** (Private visibility is fine for personal use).
5. Note the resulting path: `<YourUsername>/AlertEmitter/1`.

## 2. Import the library in a strategy

Edit `example_strategy.pine` and replace `YourTVUsername` with your TV username:

```pine
import YourTVUsername/AlertEmitter/1 as ae
```

Save the strategy to your chart normally (not as a library).

## 3. Create the webhook alert

1. Right-click the strategy panel → **Create alert on…**
2. Set **Condition** to your strategy and the desired trigger.
3. **Message field**: leave empty — `alert()` inside the strategy already sets
   the message body.
4. **Webhook URL**: paste your bot's endpoint, e.g.
   `https://your-host/webhook`.
5. Click **Create**.

> **IP whitelist**: TradingView sends webhooks from a fixed set of IPs.
> Add them to your firewall or reverse-proxy allowlist.
> See: https://www.tradingview.com/support/solutions/43000529348

## 4. Common gotchas

- **Secret mismatch** — the `Shared Secret` input must exactly match
  `HMAC_SHARED_SECRET` in the bot's `.env` file.

- **`alert.freq_once_per_bar_close`** — used here deliberately. Do **not**
  switch to `alert.freq_once_per_bar` in live trading; it can fire multiple
  times on the same bar and send duplicate orders.

- **Library version bump** — after re-publishing the library, increment the
  version number in the `import` statement to match.

- **TV placeholder substitution** — `{{ticker}}`, `{{exchange}}`, `{{time}}`,
  `{{timenow}}`, and `{{interval}}` are substituted by TradingView at
  alert-fire time. Never hardcode these values manually.

- **Unknown fields** — the bot's webhook rejects payloads with extra fields
  (`extra="forbid"` in Pydantic). Validate any custom JSON with a linter
  before deploying.
