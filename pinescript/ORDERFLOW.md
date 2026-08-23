# Order Flow for TradingView — footprint + cumulative delta

Two Pine v6 scripts that rebuild the order-flow toolkit (ATAS / Sierra / Bookmap
style) from the only microstructure data Pine can reach: **intrabar OHLCV via
`request.security_lower_tf()`**.

| File | Pane | What it gives you |
|---|---|---|
| `orderflow_footprint.pine` | overlay | Per-bar footprint ladder (Bid × Ask), diagonal + stacked imbalances, bar POC, bar value area, unfinished auctions, Δ footer, stats table |
| `orderflow_cvd.pine` | separate | CVD candles / line / Δ histogram, price↔CVD divergence, absorption, exhaustion, session Δ, stats table |

Both use the identical classification engine, so their numbers agree.

---

## 1. What is and isn't replicable

Real footprint software reads the **trade tape**: every print carries a price, a
size, and whether it hit the bid or lifted the ask. TradingView gives Pine no
tape and no book. So delta is **inferred**, not observed.

| Real order-flow feature | Here | How |
|---|---|---|
| Bid × Ask ladder per bar | ✅ approximated | Intrabar volume classified, binned into price rows |
| Bar delta, Δ max / Δ min | ✅ approximated | Running delta path across intrabars |
| Cumulative delta (CVD) | ✅ approximated | Sum of bar deltas, resettable per session |
| Diagonal + stacked imbalance | ✅ | Ask[row] vs Bid[row−1], Bid[row] vs Ask[row+1] |
| Bar POC / value area | ✅ | Exact, from the binned rows |
| Unfinished auction | ✅ | Both sides printed on an extreme row |
| Absorption / exhaustion | ✅ heuristic | Delta percentile vs realised range |
| Price↔CVD divergence | ✅ | Confirmed pivots, no repaint |
| **True aggressor side** | ❌ | Needs the tape |
| **Resting book / DOM / iceberg detection** | ❌ | Needs L2 |
| **Trade-size buckets (large-lot filtering)** | ❌ | Needs per-print size |
| **Volume-at-price from actual prints** | ⚠️ | Rows are filled from intrabar OHLC, not prints |

**Classification modes** (`Buy/sell classification`):

- **Tick rule** *(default)* — an intrabar closing above the previous intrabar's
  close is buy volume, below is sell, unchanged carries the last direction.
  This is Lee-Ready without the midpoint test and is the closest proxy to a real
  aggressor split on liquid instruments.
- **Bar polarity** — intrabar close > open ⇒ buy. Crudest; use only when the
  intrabar resolution is coarse relative to the chart TF.
- **Proportional** — splits each intrabar's volume by where it closed inside its
  own range. Smoothest, never produces a 100/0 cell, best for thin books where
  the tick rule whipsaws.

The finer the intrabar resolution, the closer any of these gets to the truth.
On 1-second intrabars over a 5-minute chart the tick rule tracks real delta
closely; on 1-minute intrabars over a 15-minute chart it is a rough sketch.

---

## 2. Install

1. TradingView → **Pine Editor** → paste `orderflow_footprint.pine` → **Add to chart**.
2. New tab in the editor → paste `orderflow_cvd.pine` → **Add to chart** (lands in its own pane).
3. Recommended chart setup: candles hollow or bars, chart TF **1m–15m**.
   The footprint needs vertical room — zoom in until bars are wide.

Nothing here talks to the bot. These are read-only analysis scripts; wire them
to a webhook by adding an `alert()` payload with the emitter in
`alert_emitter.pine` if you ever want them to trade.

---

## 3. Settings that actually matter

### Intrabar resolution (`① Data engine`)

`Auto` picks: 1S for ≤1m charts, 5S for ≤5m, 15S for ≤15m, 1m for ≤1h, 5m/15m above.

- **Seconds-based intrabars need a paid TradingView plan.** If the stats table
  reads `NO DATA` or every row shows a single number, switch to
  **Manual → `1`**.
- The total intrabar budget is plan-dependent. If the footprint stops rendering
  a few hundred bars back, that's the budget, not a bug — it doesn't matter,
  since only the last `Bars to draw` bars are drawn anyway.
- Rule of thumb: aim for **20–120 intrabars per chart bar**. The table's
  `intrabar TF` row shows the live count in brackets.

### Rows (`② Price rows`)

`Auto` sizes rows off ATR(50) so a typical bar gets `target rows per bar` rows —
this keeps the ladder legible when volatility changes instead of collapsing to
one row in the morning and 60 rows at expiry. Switch to `Manual (ticks)` when
you want a fixed grid across the whole chart:

| Instrument | Suggested rows |
|---|---|
| NIFTY / BANKNIFTY index | Auto, target 9–11 |
| Index options (premium 50–400) | Manual, 5–10 ticks |
| NATURALGAS futures | Manual, 2–5 ticks (tick 0.10) |
| CRUDEOILM futures | Manual, 1–3 ticks |
| Equity cash | Auto, target 11 |

`Hard cap on rows per bar` is a safety valve — a gap bar will not blow the
drawing budget, it just gets fatter rows.

### Drawing budget

Pine allows 500 boxes / 500 labels / 500 lines. The defaults
(10 bars × 18 rows, histogram on) sit at roughly 380 boxes and 200 labels.
If drawings start disappearing off the left:

- set `Row background` = **None** → frees ~2 boxes per row,
- or lower `Bars to draw`,
- or lower the row cap.

### Imbalances (`④`)

- `Diagonal imbalance ratio` **3.0** is the industry default; 2.0 for thin
  instruments, 4.0+ to only see the violent ones.
- `Minimum cell size (% of bar volume)` filters out one-lot tail imbalances —
  raise it on illiquid option strikes where the wings are noise.
- `Stacked imbalance = N in a row` **3** draws a level and fires an alert.
  Stacked buy imbalances mark where aggressive buyers cleared several price
  levels in sequence — those levels tend to act as support on a revisit, which
  is why the line extends forward.

### Absorption (`orderflow_cvd.pine`, ④)

Fires when |delta| is in the top `delta percentile` of the last N bars **and**
price refuses to follow (small body, or a close against the delta). Buy
absorption = aggressive buying eaten by resting sellers ⇒ bearish, and vice
versa. Exhaustion is the mirror: a wide-range bar built on almost no net delta —
the move isn't being paid for.

---

## 4. Reading the footprint

```
      1.2K ×  340       ← bid (sell) × ask (buy) at that price row
       880 × 2.9K       ← green text = diagonal BUY imbalance
```

- **Green cell tint / green text** — buy imbalance: ask at this row ≥ ratio ×
  bid one row below.
- **Red cell tint / red text** — sell imbalance.
- **Orange text** — the bar's POC row (highest total volume).
- **Orange line** — POC price. **Dotted grey box** — value area (default 70 %).
- **◆ at a bar extreme** — unfinished auction; price often returns to finish it.
- **Footer number** — the bar's net delta.

Classic reads:
- Big positive Δ but the bar closes on its low → buyers absorbed at the highs.
- POC printing near the bar low on an up bar → the move is supported.
- Stacked buy imbalances into a high with a negative Δ footer → trapped longs.

---

## 5. Alerts

Both scripts expose `alertcondition` entries (Create Alert → Condition → the
indicator → pick the trigger):

| Script | Alerts |
|---|---|
| Footprint | Stacked buy imbalance · Stacked sell imbalance · Unfinished auction · Bar delta flip |
| CVD | Bearish divergence · Bullish divergence · Buy absorption · Sell absorption · CVD zero cross |

Use **Once per bar close**. On the live bar the footprint redraws on every tick
by design (delete + redraw of the current bar only), so intrabar alerts would
fire on provisional numbers.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Table shows `NO DATA` | Lower TF not available on your plan → `Manual` = `1`. The script falls back to a one-row chart-bar estimate so the chart still renders. |
| One fat row per bar | Row size too large — lower `ticks per row`, or raise `target rows per bar`. |
| Cells overlap / unreadable | Zoom in, drop `Text size` to Tiny, or reduce rows. |
| Drawings vanish on old bars | Pine's 500-object cap. Reduce `Bars to draw` or set `Row background` = None. |
| Delta looks nothing like my broker's | Expected — inferred vs real tape. Try `Proportional`, and go finer on the intrabar TF. |
| Everything is one-sided | `Bar polarity` on coarse intrabars does this. Use `Tick rule`. |
| Slow chart | Seconds intrabars on a long history are expensive. Coarsen the intrabar TF; drawing count is already capped. |

---

## 7. Honest limits before you size off this

1. There is **no bid/ask**. Every number is a model of the tape, not the tape.
2. Indian option chains print thin at the wings — imbalance ratios there are
   frequently one-lot artefacts. Keep `Minimum cell size` ≥ 1 %.
3. Delta on **options** measures premium flow, not directional conviction in the
   underlying; a big positive delta on a PE is aggressive *put buying*, i.e.
   bearish on the underlying. Read the sign against the instrument, not the index.
4. The live bar's footprint is provisional and rewrites itself every tick.
   Historical bars never repaint.
5. Divergence and absorption labels are drawn only on confirmed bars, so what
   you see in history is what you'd have seen live.
