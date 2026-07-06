"""
Lord Loren Indicator backtest on NIFTY and BANKNIFTY futures.

Strategy logic (Pine Script port):
  SuperTrend(ATR period=10, multiplier=3.0) + EMA(20) + ADX(14) >= 20 filter
  BUY  signal: ST flips to uptrend (dir_change < 0) AND green candle AND close > EMA AND ADX >= 20
  SELL signal: ST flips to downtrend (dir_change > 0) AND red candle AND close < EMA AND ADX >= 20

Entry: signal bar close
SL   : entry ± ATR × 1.5  (BUY: entry - 1.5*ATR, SELL: entry + 1.5*ATR)
TP   : entry ± ATR × 2.5  (BUY: entry + 2.5*ATR, SELL: entry - 2.5*ATR)

One active trade at a time per underlying × timeframe.
"""

import csv
import math
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict


# ── Parameters ────────────────────────────────────────────────────────────────
ATR_PERIOD  = 10
ST_MULT     = 3.0
EMA_LEN     = 20
SL_ATR      = 1.5
TP_ATR      = 2.5
ADX_PERIOD  = 14
ADX_THRESH  = 20.0   # overridden per run below

DATA = {
    'NIFTY':     Path('data/nifty_futures/nifty_5min_futures.csv'),
    'BANKNIFTY': Path('data/banknifty_futures/banknifty_5min_futures.csv'),
}

TODAY = date(2026, 6, 13)
PERIODS = {
    '3M': TODAY - timedelta(days=91),
    '6M': TODAY - timedelta(days=182),
}

# ── Data loading ───────────────────────────────────────────────────────────────

def load_5min(path: Path) -> list[dict]:
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            dt = datetime.fromisoformat(f"{r['date']} {r['time']}")
            rows.append({
                'dt': dt,
                'open':  float(r['open']),
                'high':  float(r['high']),
                'low':   float(r['low']),
                'close': float(r['close']),
                'volume': int(r['volume']),
            })
    rows.sort(key=lambda x: x['dt'])
    return rows


def resample_15min(bars5: list[dict]) -> list[dict]:
    """Aggregate 5-min bars into 15-min bars (align to :00/:15/:30/:45 boundaries)."""
    bucket: dict = {}
    for b in bars5:
        m = b['dt'].minute
        aligned_m = (m // 15) * 15
        key = b['dt'].replace(minute=aligned_m, second=0, microsecond=0)
        if key not in bucket:
            bucket[key] = {'dt': key, 'open': b['open'], 'high': b['high'],
                           'low': b['low'], 'close': b['close'], 'volume': b['volume']}
        else:
            bk = bucket[key]
            bk['high']   = max(bk['high'],  b['high'])
            bk['low']    = min(bk['low'],   b['low'])
            bk['close']  = b['close']
            bk['volume'] += b['volume']
    return sorted(bucket.values(), key=lambda x: x['dt'])


# ── Indicator computation ──────────────────────────────────────────────────────

def wilder_atr(bars: list[dict], period: int) -> list[float]:
    n = len(bars)
    atr = [float('nan')] * n
    if n < 2:
        return atr
    tr_first = bars[0]['high'] - bars[0]['low']
    running = tr_first
    for i in range(1, n):
        h, l, pc = bars[i]['high'], bars[i]['low'], bars[i-1]['close']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if i < period:
            running += tr
            if i == period - 1:
                atr[i] = running / period
        else:
            prev = atr[i-1]
            if math.isnan(prev):
                atr[i] = float('nan')
            else:
                atr[i] = (prev * (period - 1) + tr) / period
    return atr


def compute_supertrend(bars: list[dict], period: int, mult: float) -> list[int]:
    """Returns direction list: -1 = uptrend, +1 = downtrend, 0 = not ready."""
    n = len(bars)
    atr_vals = wilder_atr(bars, period)
    direction = [0] * n
    upper = [float('nan')] * n
    lower = [float('nan')] * n

    for i in range(n):
        a = atr_vals[i]
        if math.isnan(a):
            direction[i] = 0
            continue
        hl2 = (bars[i]['high'] + bars[i]['low']) / 2
        upper[i] = hl2 + mult * a
        lower[i] = hl2 - mult * a

        if i == 0 or direction[i-1] == 0:
            direction[i] = -1  # start uptrend
            continue

        # Adjust bands to not widen against trend
        if not math.isnan(upper[i-1]):
            upper[i] = min(upper[i], upper[i-1]) if bars[i-1]['close'] < upper[i-1] else upper[i]
        if not math.isnan(lower[i-1]):
            lower[i] = max(lower[i], lower[i-1]) if bars[i-1]['close'] > lower[i-1] else lower[i]

        prev_dir = direction[i-1]
        c = bars[i]['close']
        if prev_dir == 1:   # was downtrend
            direction[i] = -1 if c > upper[i] else 1
        else:               # was uptrend
            direction[i] = 1 if c < lower[i] else -1

    return direction


def compute_ema(bars: list[dict], length: int) -> list[float]:
    n = len(bars)
    ema = [float('nan')] * n
    k = 2 / (length + 1)
    for i in range(n):
        c = bars[i]['close']
        if i < length - 1:
            ema[i] = float('nan')
        elif i == length - 1:
            ema[i] = sum(bars[j]['close'] for j in range(length)) / length
        else:
            ema[i] = c * k + ema[i-1] * (1 - k)
    return ema


def compute_adx(bars: list[dict], period: int = 14) -> list[float]:
    """Wilder-smoothed ADX (same as TradingView default)."""
    n = len(bars)
    adx = [float('nan')] * n
    if n < 2 * period + 1:
        return adx

    tr_vals, pdm_vals, ndm_vals = [], [], []
    for i in range(n):
        h, l, c = bars[i]['high'], bars[i]['low'], bars[i]['close']
        if i == 0:
            tr_vals.append(h - l)
            pdm_vals.append(0.0)
            ndm_vals.append(0.0)
            continue
        ph, pl = bars[i-1]['high'], bars[i-1]['low']
        pc = bars[i-1]['close']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up_move   = h - ph
        down_move = pl - l
        pdm = up_move   if up_move > down_move and up_move > 0   else 0.0
        ndm = down_move if down_move > up_move  and down_move > 0 else 0.0
        tr_vals.append(tr)
        pdm_vals.append(pdm)
        ndm_vals.append(ndm)

    # Wilder smooth
    alpha = 1.0 / period
    s_tr = s_pdm = s_ndm = float('nan')
    dx_vals = [float('nan')] * n

    for i in range(n):
        if i < period:
            if i == period - 1:
                s_tr  = sum(tr_vals[:period])
                s_pdm = sum(pdm_vals[:period])
                s_ndm = sum(ndm_vals[:period])
        else:
            s_tr  = s_tr  - s_tr  * alpha + tr_vals[i]
            s_pdm = s_pdm - s_pdm * alpha + pdm_vals[i]
            s_ndm = s_ndm - s_ndm * alpha + ndm_vals[i]

        if i >= period - 1 and s_tr and s_tr > 0:
            pdi = 100 * s_pdm / s_tr
            ndi = 100 * s_ndm / s_tr
            denom = pdi + ndi
            dx_vals[i] = 100 * abs(pdi - ndi) / denom if denom else 0.0

    # Smooth DX into ADX
    adx_val = float('nan')
    dx_count = 0
    dx_sum = 0.0
    for i in range(n):
        dx = dx_vals[i]
        if math.isnan(dx):
            continue
        if math.isnan(adx_val):
            dx_sum += dx
            dx_count += 1
            if dx_count == period:
                adx_val = dx_sum / period
                adx[i] = adx_val
        else:
            adx_val = adx_val * (1 - alpha) + dx * alpha
            adx[i] = adx_val

    return adx


# ── Backtest engine ────────────────────────────────────────────────────────────

def last_bar_of_day(bars: list[dict]) -> set:
    """Returns set of bar datetimes that are the last bar of their trading day."""
    last = {}
    for b in bars:
        d = b['dt'].date()
        last[d] = b['dt']
    return set(last.values())


def backtest(bars: list[dict], start_date: date) -> dict:
    """SL/TP active intraday; any open trade is closed at EOD price (no overnight carry)."""
    if not bars:
        return {'trades': [], 'stats': {}}

    dir_vals  = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    atr_vals  = wilder_atr(bars, ATR_PERIOD)
    adx_vals  = compute_adx(bars, ADX_PERIOD)
    eod_times = last_bar_of_day(bars)

    trades   = []
    in_trade = None

    for i in range(1, len(bars)):
        b = bars[i]
        if b['dt'].date() < start_date:
            continue

        d   = dir_vals[i]
        dp  = dir_vals[i - 1]
        atr = atr_vals[i]
        adx = adx_vals[i]

        if d == 0 or math.isnan(atr) or atr == 0:
            continue

        c, h, l = b['close'], b['high'], b['low']
        is_eod  = b['dt'] in eod_times

        # ── Check exit ────────────────────────────────────────────
        if in_trade:
            side  = in_trade['side']
            entry = in_trade['entry']
            sl    = in_trade['sl']
            tp    = in_trade['tp']

            hit_sl = (side == 'BUY' and l <= sl) or (side == 'SELL' and h >= sl)
            hit_tp = (side == 'BUY' and h >= tp) or (side == 'SELL' and l <= tp)

            if hit_sl and hit_tp:
                exit_price, exit_type = sl, 'SL'   # assume SL hit first (worst case)
            elif hit_sl:
                exit_price, exit_type = sl, 'SL'
            elif hit_tp:
                exit_price, exit_type = tp, 'TP'
            elif is_eod:
                exit_price, exit_type = c, 'EOD'
            else:
                exit_price = None

            if exit_price is not None:
                pnl = (exit_price - entry) if side == 'BUY' else (entry - exit_price)
                trades.append({
                    'entry_dt':  in_trade['entry_dt'],
                    'exit_dt':   b['dt'],
                    'side':      side,
                    'entry':     entry,
                    'exit':      exit_price,
                    'exit_type': exit_type,
                    'pnl_pts':   round(pnl, 2),
                })
                in_trade = None

        # ── Check entry (no new entry on EOD bar, no overnight carry) ──
        if in_trade is None and not is_eod:
            adx_ok = not math.isnan(adx) and adx >= ADX_THRESH
            if adx_ok and d != dp:
                if d == -1:   # ST flips uptrend → BUY
                    sl = c - SL_ATR * atr
                    tp = c + TP_ATR * atr
                    in_trade = {'side': 'BUY', 'entry': c, 'sl': sl, 'tp': tp,
                                'entry_dt': b['dt'], 'atr': atr}
                elif d == 1:  # ST flips downtrend → SELL
                    sl = c + SL_ATR * atr
                    tp = c - TP_ATR * atr
                    in_trade = {'side': 'SELL', 'entry': c, 'sl': sl, 'tp': tp,
                                'entry_dt': b['dt'], 'atr': atr}

    return compute_stats(trades)


def compute_stats(trades: list[dict]) -> dict:
    if not trades:
        return {'trades': [], 'stats': {'total': 0}}
    wins   = [t for t in trades if t['pnl_pts'] > 0]
    losses = [t for t in trades if t['pnl_pts'] <= 0]
    total_pnl = sum(t['pnl_pts'] for t in trades)
    stats = {
        'total':         len(trades),
        'wins':          len(wins),
        'losses':        len(losses),
        'win_rate':      round(100 * len(wins) / len(trades), 1),
        'total_pnl_pts': round(total_pnl, 2),
        'avg_win_pts':   round(sum(t['pnl_pts'] for t in wins)   / len(wins),   2) if wins   else 0,
        'avg_loss_pts':  round(sum(t['pnl_pts'] for t in losses) / len(losses), 2) if losses else 0,
    }
    return {'trades': trades, 'stats': stats}


# ── Main ───────────────────────────────────────────────────────────────────────

from collections import defaultdict as _dd

def run_one(adx_thresh: float) -> dict:
    global ADX_THRESH
    ADX_THRESH = adx_thresh
    results = {}
    for symbol, path in DATA.items():
        bars5  = load_5min(path)
        bars15 = resample_15min(bars5)
        results[symbol] = {}
        for period_name, start in PERIODS.items():
            results[symbol][period_name] = {}
            for tf_name, bars in [('5min', bars5), ('15min', bars15)]:
                r = backtest(bars, start)
                s = r['stats']
                monthly     = _dd(float)
                monthly_tr  = _dd(int)
                monthly_w   = _dd(int)
                for t in r['trades']:
                    ym = t['entry_dt'].strftime('%Y-%m')
                    monthly[ym]    += t['pnl_pts']
                    monthly_tr[ym] += 1
                    if t['pnl_pts'] > 0:
                        monthly_w[ym] += 1
                results[symbol][period_name][tf_name] = {
                    'stats': s,
                    'monthly': dict(monthly),
                    'monthly_tr': dict(monthly_tr),
                    'monthly_w': dict(monthly_w),
                }
    return results


def print_comparison(all_results: dict):
    thresholds = sorted(all_results.keys())
    col_w = 20   # width per threshold column

    for symbol in DATA:
        for period_name in PERIODS:
            for tf_name in ['5min', '15min']:
                print(f"\n[{symbol}] [{period_name}] [{tf_name}]")
                # header
                print(f"  {'Month':<10}", end='')
                for th in thresholds:
                    hdr = f"ADX>={th}"
                    print(f"  {hdr:>7}  {'Tr':>3}  {'W%':>4}", end='')
                print()
                print(f"  {'-'*58}")

                # collect all months
                all_months = set()
                for th in thresholds:
                    r = all_results[th][symbol][period_name][tf_name]
                    all_months.update(r['monthly'].keys())

                for ym in sorted(all_months):
                    print(f"  {ym:<10}", end='')
                    for th in thresholds:
                        r = all_results[th][symbol][period_name][tf_name]
                        pnl = r['monthly'].get(ym, 0.0)
                        tr  = r['monthly_tr'].get(ym, 0)
                        w   = r['monthly_w'].get(ym, 0)
                        wp  = int(100 * w / tr) if tr else 0
                        print(f"  {pnl:>+7.0f}  {tr:>3}  {wp:>3}%", end='')
                    print()

                # totals
                print(f"  {'TOTAL':<10}", end='')
                for th in thresholds:
                    s = all_results[th][symbol][period_name][tf_name]['stats']
                    pnl = s.get('total_pnl_pts', 0)
                    tr  = s.get('total', 0)
                    wr  = s.get('win_rate', 0)
                    print(f"  {pnl:>+7.0f}  {tr:>3}  {wr:>3.0f}%", end='')
                print()


if __name__ == '__main__':
    all_results = {}
    for thresh in [20, 25, 30]:
        print(f"Running ADX>={thresh}...", flush=True)
        all_results[thresh] = run_one(thresh)
    print("\nDone. Printing comparison...\n")
    print_comparison(all_results)
