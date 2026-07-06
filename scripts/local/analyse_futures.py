"""
Analyse 6M BANKNIFTY 15-min futures data and backtest 2 improved strategies.

Strategy 1: Regime-Filtered SuperTrend
  - ADX >= 25 → trending → trade ST flips (same as baseline)
  - ADX < 20 → ranging → trade Bollinger Band mean reversion

Strategy 2: Opening Range Breakout (ORB)
  - First 30-min high/low = range
  - Break above range high → Long (SL = range low, TP = entry + 2×range_size)
  - Break below range low → Short (SL = range high, TP = entry - 2×range_size)
  - Only one trade per day, EOD exit if not triggered

Baseline for comparison: pure SuperTrend flip, ADX >= 25 (from previous backtest)
"""

import csv, math
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

# ─── Parameters ───────────────────────────────────────────────────────────────
ATR_PERIOD  = 10
ST_MULT     = 3.0
ADX_PERIOD  = 14
BB_PERIOD   = 20
BB_MULT     = 2.0
ORB_BARS    = 6          # 6 × 5-min = 30 min opening range
SL_ATR      = 1.5
TP_ATR      = 3.0        # 1:2 RR

START_6M    = date(2026, 5, 26) - timedelta(days=182)   # 2025-11-25
END_DATE    = date(2026, 5, 26)

FUTURES_CSV = Path('data/banknifty_futures/banknifty_5min_futures.csv')

# ─── Load & resample ──────────────────────────────────────────────────────────
def load_5min(path: Path, start: date, end: date) -> list[dict]:
    bars = []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r['date'])
            if d < start or d > end:
                continue
            dt = datetime.fromisoformat(f"{r['date']}T{r['time']}")
            bars.append({'dt': dt, 'date': d,
                         'open': float(r['open']), 'high': float(r['high']),
                         'low': float(r['low']),   'close': float(r['close']),
                         'volume': int(r['volume'])})
    bars.sort(key=lambda b: b['dt'])
    return bars


def to_15min(bars5: list[dict]) -> list[dict]:
    buckets = {}
    for b in bars5:
        m = b['dt'].minute
        slot = (m // 15) * 15
        key = b['dt'].replace(minute=slot, second=0)
        if key not in buckets:
            buckets[key] = {'dt': key, 'date': b['date'],
                             'open': b['open'], 'high': b['high'],
                             'low': b['low'],   'close': b['close'],
                             'volume': b['volume']}
        else:
            buckets[key]['high']   = max(buckets[key]['high'], b['high'])
            buckets[key]['low']    = min(buckets[key]['low'],  b['low'])
            buckets[key]['close']  = b['close']
            buckets[key]['volume'] += b['volume']
    out = sorted(buckets.values(), key=lambda b: b['dt'])
    return out


# ─── Indicators ───────────────────────────────────────────────────────────────
def wilder_atr(bars, period):
    n = len(bars)
    atr = [float('nan')] * n
    if n < 2:
        return atr
    trs = [0.0] * n
    trs[0] = bars[0]['high'] - bars[0]['low']
    for i in range(1, n):
        tr = max(bars[i]['high'] - bars[i]['low'],
                 abs(bars[i]['high'] - bars[i-1]['close']),
                 abs(bars[i]['low']  - bars[i-1]['close']))
        trs[i] = tr
    atr[period-1] = sum(trs[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + trs[i]) / period
    return atr


def compute_supertrend(bars, period, mult):
    n   = len(bars)
    atr = wilder_atr(bars, period)
    ub  = [float('nan')] * n
    lb  = [float('nan')] * n
    dir_= [0] * n
    for i in range(period - 1, n):
        hl2 = (bars[i]['high'] + bars[i]['low']) / 2
        ub[i] = hl2 + mult * atr[i]
        lb[i] = hl2 - mult * atr[i]
        if i == period - 1:
            dir_[i] = 1
            continue
        prev_ub = ub[i-1]
        prev_lb = lb[i-1]
        if not math.isnan(prev_ub):
            ub[i] = min(ub[i], prev_ub) if bars[i-1]['close'] < prev_ub else ub[i]
            lb[i] = max(lb[i], prev_lb) if bars[i-1]['close'] > prev_lb else lb[i]
        if dir_[i-1] == 1:
            dir_[i] = -1 if bars[i]['close'] > ub[i-1] else 1
        else:
            dir_[i] =  1 if bars[i]['close'] < lb[i-1] else -1
    return dir_, atr, ub, lb


def compute_adx(bars, period=14):
    n = len(bars)
    adx = [float('nan')] * n
    if n < 2 * period + 1:
        return adx
    pdm = [0.0] * n
    ndm = [0.0] * n
    tr  = [0.0] * n
    for i in range(1, n):
        up   = bars[i]['high'] - bars[i-1]['high']
        down = bars[i-1]['low'] - bars[i]['low']
        pdm[i] = up   if up > down and up > 0 else 0.0
        ndm[i] = down if down > up and down > 0 else 0.0
        tr[i]  = max(bars[i]['high'] - bars[i]['low'],
                     abs(bars[i]['high'] - bars[i-1]['close']),
                     abs(bars[i]['low']  - bars[i-1]['close']))
    alpha = 1.0 / period
    apdm = sum(pdm[1:period+1])
    andm = sum(ndm[1:period+1])
    atr  = sum(tr[1:period+1])
    dx_vals = []
    for i in range(period + 1, n):
        apdm = apdm * (1 - alpha) + pdm[i]
        andm = andm * (1 - alpha) + ndm[i]
        atr  = atr  * (1 - alpha) + tr[i]
        pdi = 100 * apdm / atr if atr else 0
        ndi = 100 * andm / atr if atr else 0
        denom = pdi + ndi
        dx = 100 * abs(pdi - ndi) / denom if denom else 0
        dx_vals.append(dx)
        if len(dx_vals) >= period:
            adx[i] = sum(dx_vals[-period:]) / period
    return adx


def compute_ema(values, length):
    out = [float('nan')] * len(values)
    alpha = 2.0 / (length + 1)
    for i, v in enumerate(values):
        if math.isnan(v):
            continue
        if math.isnan(out[i-1]) if i > 0 else True:
            out[i] = v
        else:
            out[i] = alpha * v + (1 - alpha) * out[i-1]
    return out


def compute_bollinger(bars, period=20, mult=2.0):
    n      = len(bars)
    mid    = [float('nan')] * n
    upper  = [float('nan')] * n
    lower  = [float('nan')] * n
    for i in range(period - 1, n):
        closes = [bars[j]['close'] for j in range(i - period + 1, i + 1)]
        m = sum(closes) / period
        std = math.sqrt(sum((c - m)**2 for c in closes) / period)
        mid[i]   = m
        upper[i] = m + mult * std
        lower[i] = m - mult * std
    return mid, upper, lower


def last_bar_of_day(bars):
    last = {}
    for b in bars:
        last[b['date']] = b['dt']
    return set(last.values())


# ─── PnL helpers ──────────────────────────────────────────────────────────────
def summarise(trades, label):
    if not trades:
        print(f"  {label}: no trades")
        return
    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    total  = sum(t['pnl'] for t in trades)
    print(f"\n  {label}  ({len(trades)} trades, {len(wins)/len(trades)*100:.0f}% win)")
    by_month = defaultdict(list)
    for t in trades:
        by_month[t['entry'].strftime('%Y-%m')].append(t['pnl'])
    print(f"  {'Month':<10}  {'Tr':>4}  {'W%':>5}  {'PnL (pts)':>10}")
    print(f"  {'-'*42}")
    for ym in sorted(by_month):
        pts  = by_month[ym]
        ww   = sum(1 for p in pts if p > 0)
        bars_str = ('+' * round(sum(pts)/50)) if sum(pts) > 0 else ('-' * round(-sum(pts)/50))
        print(f"  {ym:<10}  {len(pts):>4}  {ww/len(pts)*100:>4.0f}%  {sum(pts):>+10.1f}  {bars_str}")
    print(f"  {'TOTAL':<10}  {len(trades):>4}  {len(wins)/len(trades)*100:>4.0f}%  {total:>+10.1f}")
    if wins:   print(f"  Avg win:  {sum(t['pnl'] for t in wins)/len(wins):>+.1f} pts")
    if losses: print(f"  Avg loss: {sum(t['pnl'] for t in losses)/len(losses):>+.1f} pts")


# ─── Strategy 0: Baseline (pure ST flip, ADX>=25) ────────────────────────────
def baseline_st(bars):
    dir_, atr_v, ub, lb = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    adx_v  = compute_adx(bars, ADX_PERIOD)
    eod_set = last_bar_of_day(bars)
    trades = []
    in_trade = None
    for i in range(max(ATR_PERIOD, ADX_PERIOD * 2 + 1), len(bars)):
        b = bars[i]
        c, h, l = b['close'], b['high'], b['low']
        d, dp   = dir_[i], dir_[i-1]
        atr     = atr_v[i]
        adx     = adx_v[i]
        adx_ok  = not math.isnan(adx) and adx >= 25
        is_eod  = b['dt'] in eod_set

        if in_trade:
            sl, tp, side = in_trade['sl'], in_trade['tp'], in_trade['side']
            hit_sl = (side == 'BUY'  and l <= sl) or (side == 'SELL' and h >= sl)
            hit_tp = (side == 'BUY'  and h >= tp) or (side == 'SELL' and l <= tp)
            if hit_sl or hit_tp or is_eod:
                ep = sl if hit_sl else (tp if hit_tp else c)
                pnl = (ep - in_trade['ep']) if side == 'BUY' else (in_trade['ep'] - ep)
                trades.append({'entry': in_trade['dt'], 'pnl': pnl,
                                'type': 'SL' if hit_sl else ('TP' if hit_tp else 'EOD'),
                                'side': side})
                in_trade = None

        if in_trade is None and not is_eod and d != dp and adx_ok:
            if d == -1:
                in_trade = {'side': 'BUY',  'ep': c, 'sl': c - SL_ATR*atr, 'tp': c + TP_ATR*atr, 'dt': b['dt']}
            elif d == 1:
                in_trade = {'side': 'SELL', 'ep': c, 'sl': c + SL_ATR*atr, 'tp': c - TP_ATR*atr, 'dt': b['dt']}
    return trades


# ─── Strategy 1: Regime-Filtered (ST in trend, BB in range) ──────────────────
def strategy_regime(bars):
    """
    ADX >= 25 (trending) → trade SuperTrend flips
    ADX < 20  (ranging)  → trade BB mean reversion (touch lower band → BUY; upper → SELL)
    ADX 20-25: no trade (ambiguous zone)
    Max 1 open trade at a time. EOD exit.
    """
    dir_, atr_v, ub, lb = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    adx_v               = compute_adx(bars, ADX_PERIOD)
    bb_mid, bb_up, bb_lo = compute_bollinger(bars, BB_PERIOD, BB_MULT)
    eod_set             = last_bar_of_day(bars)
    trades = []
    in_trade = None

    for i in range(max(ATR_PERIOD, ADX_PERIOD * 2 + 1, BB_PERIOD), len(bars)):
        b   = bars[i]
        c, h, l, o = b['close'], b['high'], b['low'], b['open']
        d, dp       = dir_[i], dir_[i-1]
        atr         = atr_v[i]
        adx         = adx_v[i]
        is_eod      = b['dt'] in eod_set

        if math.isnan(adx) or math.isnan(atr) or math.isnan(bb_lo[i]):
            continue

        # ── Exit ──
        if in_trade:
            sl, tp, side = in_trade['sl'], in_trade['tp'], in_trade['side']
            hit_sl = (side == 'BUY'  and l <= sl) or (side == 'SELL' and h >= sl)
            hit_tp = (side == 'BUY'  and h >= tp) or (side == 'SELL' and l <= tp)
            if hit_sl or hit_tp or is_eod:
                ep = sl if hit_sl else (tp if hit_tp else c)
                pnl = (ep - in_trade['ep']) if side == 'BUY' else (in_trade['ep'] - ep)
                trades.append({'entry': in_trade['dt'], 'pnl': pnl,
                                'type': 'SL' if hit_sl else ('TP' if hit_tp else 'EOD'),
                                'regime': in_trade['regime'], 'side': side})
                in_trade = None

        if in_trade is not None or is_eod:
            continue

        # ── Trending regime: ST flip ──
        if adx >= 25 and d != dp:
            if d == -1:
                in_trade = {'side': 'BUY',  'ep': c, 'sl': c - SL_ATR*atr,
                             'tp': c + TP_ATR*atr, 'dt': b['dt'], 'regime': 'TREND'}
            elif d == 1:
                in_trade = {'side': 'SELL', 'ep': c, 'sl': c + SL_ATR*atr,
                             'tp': c - TP_ATR*atr, 'dt': b['dt'], 'regime': 'TREND'}

        # ── Ranging regime: BB touch with reversal candle ──
        elif adx < 20:
            prev_c = bars[i-1]['close']
            # Price touches/pierces lower band and closes back above → buy
            if l <= bb_lo[i] and c > bb_lo[i] and c > o:
                sl = c - SL_ATR * atr
                tp = bb_mid[i]                        # target = midline
                if tp - c > SL_ATR * atr * 0.5:      # min reward filter
                    in_trade = {'side': 'BUY', 'ep': c, 'sl': sl, 'tp': tp,
                                 'dt': b['dt'], 'regime': 'RANGE'}
            # Price touches/pierces upper band and closes back below → sell
            elif h >= bb_up[i] and c < bb_up[i] and c < o:
                sl = c + SL_ATR * atr
                tp = bb_mid[i]
                if c - tp > SL_ATR * atr * 0.5:
                    in_trade = {'side': 'SELL', 'ep': c, 'sl': sl, 'tp': tp,
                                 'dt': b['dt'], 'regime': 'RANGE'}
    return trades


# ─── Strategy 2: Opening Range Breakout (ORB) ────────────────────────────────
def strategy_orb(bars):
    """
    Every trading day:
    1. Build opening range from first ORB_BARS 15-min bars (= first 30 min if 15-min TF, 30 min if 5-min)
    2. First break above range high → BUY  (SL = range low,  TP = entry + 2×range_size)
    3. First break below range low  → SELL (SL = range high, TP = entry - 2×range_size)
    4. Only one trade per day. EOD exit if not filled.
    """
    eod_set = last_bar_of_day(bars)
    by_day  = defaultdict(list)
    for b in bars:
        by_day[b['date']].append(b)
    trades = []

    for d in sorted(by_day):
        day_bars = by_day[d]
        if len(day_bars) < ORB_BARS + 2:
            continue
        orb_bars = day_bars[:ORB_BARS]
        orb_high = max(b['high']  for b in orb_bars)
        orb_low  = min(b['low']   for b in orb_bars)
        rng      = orb_high - orb_low
        if rng < 50:            # filter tiny ranges (holiday/gap days)
            continue

        in_trade  = None
        triggered = False
        for b in day_bars[ORB_BARS:]:
            c, h, l = b['close'], b['high'], b['low']
            is_eod  = b['dt'] in eod_set

            if in_trade:
                sl, tp, side = in_trade['sl'], in_trade['tp'], in_trade['side']
                hit_sl = (side == 'BUY'  and l <= sl) or (side == 'SELL' and h >= sl)
                hit_tp = (side == 'BUY'  and h >= tp) or (side == 'SELL' and l <= tp)
                if hit_sl or hit_tp or is_eod:
                    ep = sl if hit_sl else (tp if hit_tp else c)
                    pnl = (ep - in_trade['ep']) if side == 'BUY' else (in_trade['ep'] - ep)
                    trades.append({'entry': in_trade['dt'], 'pnl': pnl,
                                    'type': 'SL' if hit_sl else ('TP' if hit_tp else 'EOD'),
                                    'side': side, 'rng': rng})
                    break
            elif not triggered:
                if h > orb_high:
                    ep = orb_high
                    sl = orb_low
                    tp = ep + 2.0 * rng
                    in_trade = {'side': 'BUY', 'ep': ep, 'sl': sl, 'tp': tp, 'dt': b['dt']}
                    triggered = True
                elif l < orb_low:
                    ep = orb_low
                    sl = orb_high
                    tp = ep - 2.0 * rng
                    in_trade = {'side': 'SELL', 'ep': ep, 'sl': sl, 'tp': tp, 'dt': b['dt']}
                    triggered = True

    return trades


# ─── Strategy 3: Pullback after ST flip (better entry) ───────────────────────
def strategy_pullback(bars):
    """
    Wait for SuperTrend to flip (trending, ADX>=25), then enter on the FIRST
    pullback to the 20-EMA (instead of entering on the flip bar itself).
    SL = below recent swing low (1.5×ATR below entry).
    TP = 3.0×ATR above entry.
    This improves RR by avoiding "buy at the top of the flip candle."
    Pullback window: up to 5 bars after flip; if not filled → skip.
    """
    dir_, atr_v, ub, lb = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    adx_v    = compute_adx(bars, ADX_PERIOD)
    closes   = [b['close'] for b in bars]
    ema_v    = compute_ema(closes, 20)
    eod_set  = last_bar_of_day(bars)
    trades   = []
    in_trade = None
    pending  = None     # {'side', 'ema_at_flip', 'atr', 'bars_left', 'dt'}
    WINDOW   = 6

    for i in range(max(ATR_PERIOD, ADX_PERIOD * 2 + 1, 20), len(bars)):
        b   = bars[i]
        c, h, l = b['close'], b['high'], b['low']
        d, dp   = dir_[i], dir_[i-1]
        atr     = atr_v[i]
        adx     = adx_v[i]
        is_eod  = b['dt'] in eod_set

        if math.isnan(adx) or math.isnan(atr) or math.isnan(ema_v[i]):
            continue

        # ── Exit ──
        if in_trade:
            sl, tp, side = in_trade['sl'], in_trade['tp'], in_trade['side']
            hit_sl = (side == 'BUY'  and l <= sl) or (side == 'SELL' and h >= sl)
            hit_tp = (side == 'BUY'  and h >= tp) or (side == 'SELL' and l <= tp)
            if hit_sl or hit_tp or is_eod:
                ep = sl if hit_sl else (tp if hit_tp else c)
                pnl = (ep - in_trade['ep']) if side == 'BUY' else (in_trade['ep'] - ep)
                trades.append({'entry': in_trade['dt'], 'pnl': pnl,
                                'type': 'SL' if hit_sl else ('TP' if hit_tp else 'EOD'),
                                'side': side})
                in_trade = None
                pending  = None

        if in_trade is not None or is_eod:
            pending = None if is_eod else pending
            continue

        # ── New ST flip creates a pending setup ──
        if adx >= 25 and d != dp and pending is None:
            side = 'BUY' if d == -1 else 'SELL'
            pending = {'side': side, 'ema': ema_v[i], 'atr': atr,
                        'bars_left': WINDOW, 'flip_bar': i}

        # ── Pending: look for pullback to EMA ──
        if pending:
            pending['bars_left'] -= 1
            side = pending['side']
            ema_now = ema_v[i]
            # BUY setup: price pulls back to touch EMA from above
            if side == 'BUY' and l <= ema_now and c > ema_now:
                ep = c
                in_trade = {'side': 'BUY', 'ep': ep,
                             'sl': ep - SL_ATR * pending['atr'],
                             'tp': ep + TP_ATR * pending['atr'], 'dt': b['dt']}
                pending = None
            # SELL setup: price rallies to touch EMA from below
            elif side == 'SELL' and h >= ema_now and c < ema_now:
                ep = c
                in_trade = {'side': 'SELL', 'ep': ep,
                             'sl': ep + SL_ATR * pending['atr'],
                             'tp': ep - TP_ATR * pending['atr'], 'dt': b['dt']}
                pending = None
            elif pending['bars_left'] <= 0:
                pending = None

    return trades


# ─── Market regime analysis ───────────────────────────────────────────────────
def analyse_market(bars):
    dir_, atr_v, ub, lb = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    adx_v  = compute_adx(bars, ADX_PERIOD)
    closes = [b['close'] for b in bars]
    ema_v  = compute_ema(closes, 20)

    trending_bars = ranging_bars = ambig_bars = 0
    for i, adx in enumerate(adx_v):
        if math.isnan(adx): continue
        if adx >= 25:   trending_bars += 1
        elif adx < 20:  ranging_bars  += 1
        else:           ambig_bars    += 1

    total = trending_bars + ranging_bars + ambig_bars
    avg_atr  = sum(a for a in atr_v if not math.isnan(a)) / sum(1 for a in atr_v if not math.isnan(a))
    avg_adx  = sum(a for a in adx_v if not math.isnan(a)) / sum(1 for a in adx_v if not math.isnan(a))

    print(f"\n{'='*58}")
    print(f"  BANKNIFTY 15-min  |  6M Market Regime Analysis")
    print(f"  Period: {bars[0]['date']} to {bars[-1]['date']}")
    print(f"{'='*58}")
    print(f"  Total bars  : {total:,}")
    print(f"  Avg ATR     : {avg_atr:.0f} pts")
    print(f"  Avg ADX     : {avg_adx:.1f}")
    print(f"  Trending (ADX>=25): {trending_bars:>5} bars  ({trending_bars/total*100:.0f}%)")
    print(f"  Ambiguous (20-25) : {ambig_bars:>5} bars  ({ambig_bars/total*100:.0f}%)")
    print(f"  Ranging   (ADX<20): {ranging_bars:>5} bars  ({ranging_bars/total*100:.0f}%)")

    # Monthly ATR and regime breakdown
    print(f"\n  Month-by-month regime:")
    print(f"  {'Month':<10}  {'AvgATR':>7}  {'AvgADX':>7}  {'Trend%':>7}  {'Range%':>7}")
    by_month = defaultdict(list)
    for i, b in enumerate(bars):
        if math.isnan(adx_v[i]) or math.isnan(atr_v[i]): continue
        by_month[b['date'].strftime('%Y-%m')].append((adx_v[i], atr_v[i]))
    for ym in sorted(by_month):
        vals = by_month[ym]
        ma   = sum(v[0] for v in vals) / len(vals)
        matr = sum(v[1] for v in vals) / len(vals)
        tr   = sum(1 for v in vals if v[0] >= 25) / len(vals) * 100
        rng  = sum(1 for v in vals if v[0] < 20)  / len(vals) * 100
        print(f"  {ym:<10}  {matr:>7.0f}  {ma:>7.1f}  {tr:>6.0f}%  {rng:>6.0f}%")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("Loading data...")
    bars5   = load_5min(FUTURES_CSV, START_6M, END_DATE)
    bars15  = to_15min(bars5)
    print(f"  5-min: {len(bars5):,}  |  15-min: {len(bars15):,}")

    analyse_market(bars15)

    print(f"\n{'='*58}")
    print(f"  STRATEGY COMPARISON — 15-min BANKNIFTY 6M")
    print(f"{'='*58}")

    t0 = baseline_st(bars15)
    summarise(t0, "BASELINE: Pure SuperTrend flip (ADX>=25)")

    t1 = strategy_regime(bars15)
    trend_trades = [t for t in t1 if t.get('regime') == 'TREND']
    range_trades = [t for t in t1 if t.get('regime') == 'RANGE']
    summarise(t1, "STRATEGY 1: Regime Filter (ST trend + BB range)")
    print(f"    [Trend leg: {len(trend_trades)} trades | Range leg: {len(range_trades)} trades]")

    t2 = strategy_orb(bars15)
    summarise(t2, "STRATEGY 2: Opening Range Breakout (30-min ORB)")

    t3 = strategy_pullback(bars15)
    summarise(t3, "STRATEGY 3: Pullback-to-EMA entry after ST flip")

    # ── Summary comparison table ──
    print(f"\n{'='*58}")
    print(f"  QUICK COMPARISON")
    print(f"  {'Strategy':<40} {'Tr':>4}  {'Win%':>5}  {'PnL (pts)':>10}")
    print(f"  {'-'*60}")
    for label, trades in [
        ("Baseline: ST flip ADX>=25", t0),
        ("Strategy 1: Regime (ST+BB)", t1),
        ("Strategy 2: ORB 30-min", t2),
        ("Strategy 3: Pullback-to-EMA", t3),
    ]:
        if not trades:
            print(f"  {label:<40} {'–':>4}  {'–':>5}  {'–':>10}")
            continue
        wins  = sum(1 for t in trades if t['pnl'] > 0)
        total = sum(t['pnl'] for t in trades)
        print(f"  {label:<40} {len(trades):>4}  {wins/len(trades)*100:>4.0f}%  {total:>+10.1f}")
    print()


if __name__ == '__main__':
    main()
