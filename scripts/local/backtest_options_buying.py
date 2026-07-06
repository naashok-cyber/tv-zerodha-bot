"""
Backtest BUYING slightly ITM options on BANKNIFTY 15-min signals.

Signal sources tested:
  A) Baseline SuperTrend flip (ADX>=25) — 31 trades
  B) Pullback-to-EMA after ST flip (ADX>=25) — 14 trades, 57% win

Option types:
  ATM   = nearest 100pt strike to futures price
  ITM1  = 1 strike (100pt) inside the money
  ITM2  = 2 strikes (200pt) inside the money

Direction:
  BUY signal  → Buy CE  (ITM = strike below futures price)
  SELL signal → Buy PE  (ITM = strike above futures price)

Exit logic (underlying-based, same as selling backtest):
  SL = underlying moves SL_ATR × ATR against us (EOD if not hit)
  TP = underlying moves TP_ATR × ATR in our favour
  EOD exit at market if neither hit by end of day

PnL = (exit_option_premium - entry_option_premium) × LOT_SIZE
"""

import csv, math, re
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

# ── Parameters ────────────────────────────────────────────────────────────────
ATR_PERIOD = 10
ST_MULT    = 3.0
ADX_PERIOD = 14
SL_ATR     = 1.0   # tighter for option buying (theta hurts when stuck)
TP_ATR     = 2.0   # 1:2 RR on underlying move
LOT_SIZE   = 15
RISK_FREE  = 0.07

START_6M   = date(2026, 5, 26) - timedelta(days=182)   # 2025-11-25
END_DATE   = date(2026, 5, 26)

FUTURES_CSV = Path('data/banknifty_futures/banknifty_5min_futures.csv')
OPTS_ROOT   = Path('data/banknifty_options')

# ── Data loading ──────────────────────────────────────────────────────────────
def load_5min(path, start, end):
    bars = []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r['date'])
            if d < start or d > end: continue
            dt = datetime.fromisoformat(f"{r['date']}T{r['time']}")
            bars.append({'dt': dt, 'date': d,
                         'open': float(r['open']), 'high': float(r['high']),
                         'low':  float(r['low']),  'close': float(r['close'])})
    return sorted(bars, key=lambda b: b['dt'])


def to_15min(bars5):
    buckets = {}
    for b in bars5:
        m    = b['dt'].minute
        slot = (m // 15) * 15
        key  = b['dt'].replace(minute=slot, second=0)
        if key not in buckets:
            buckets[key] = {**b, 'dt': key}
        else:
            buckets[key]['high']  = max(buckets[key]['high'], b['high'])
            buckets[key]['low']   = min(buckets[key]['low'],  b['low'])
            buckets[key]['close'] = b['close']
    return sorted(buckets.values(), key=lambda b: b['dt'])


# ── Indicators ────────────────────────────────────────────────────────────────
def wilder_atr(bars, period):
    n = len(bars); atr = [float('nan')] * n
    trs = [bars[0]['high'] - bars[0]['low']] + [
        max(bars[i]['high'] - bars[i]['low'],
            abs(bars[i]['high'] - bars[i-1]['close']),
            abs(bars[i]['low']  - bars[i-1]['close']))
        for i in range(1, n)]
    atr[period-1] = sum(trs[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period-1) + trs[i]) / period
    return atr


def compute_supertrend(bars, period, mult):
    n   = len(bars)
    atr = wilder_atr(bars, period)
    ub  = [float('nan')] * n
    lb  = [float('nan')] * n
    dir_= [0] * n
    for i in range(period-1, n):
        hl2    = (bars[i]['high'] + bars[i]['low']) / 2
        ub[i]  = hl2 + mult * atr[i]
        lb[i]  = hl2 - mult * atr[i]
        if i == period-1:
            dir_[i] = 1; continue
        ub[i] = min(ub[i], ub[i-1]) if bars[i-1]['close'] < ub[i-1] else ub[i]
        lb[i] = max(lb[i], lb[i-1]) if bars[i-1]['close'] > lb[i-1] else lb[i]
        if dir_[i-1] == 1:
            dir_[i] = -1 if bars[i]['close'] > ub[i-1] else 1
        else:
            dir_[i] =  1 if bars[i]['close'] < lb[i-1] else -1
    return dir_, atr


def compute_adx(bars, period=14):
    n   = len(bars)
    adx = [float('nan')] * n
    if n < 2*period+1: return adx
    pdm = ndm = tr = [0.0]*n
    pdm = [0.0]*n; ndm = [0.0]*n; tr = [0.0]*n
    for i in range(1, n):
        up   = bars[i]['high'] - bars[i-1]['high']
        down = bars[i-1]['low'] - bars[i]['low']
        pdm[i] = up   if up > down and up > 0   else 0.0
        ndm[i] = down if down > up and down > 0 else 0.0
        tr[i]  = max(bars[i]['high'] - bars[i]['low'],
                     abs(bars[i]['high'] - bars[i-1]['close']),
                     abs(bars[i]['low']  - bars[i-1]['close']))
    alpha = 1.0/period
    ap = sum(pdm[1:period+1]); an = sum(ndm[1:period+1]); at = sum(tr[1:period+1])
    dx_vals = []
    for i in range(period+1, n):
        ap = ap*(1-alpha)+pdm[i]; an = an*(1-alpha)+ndm[i]; at = at*(1-alpha)+tr[i]
        pdi = 100*ap/at if at else 0; ndi = 100*an/at if at else 0
        denom = pdi+ndi
        dx_vals.append(100*abs(pdi-ndi)/denom if denom else 0)
        if len(dx_vals) >= period:
            adx[i] = sum(dx_vals[-period:]) / period
    return adx


def compute_ema(vals, length):
    out = [float('nan')] * len(vals); alpha = 2.0/(length+1)
    for i, v in enumerate(vals):
        if math.isnan(v): continue
        out[i] = v if (i==0 or math.isnan(out[i-1])) else alpha*v + (1-alpha)*out[i-1]
    return out


def last_bar_of_day(bars):
    last = {}
    for b in bars: last[b['date']] = b['dt']
    return set(last.values())


# ── Options index ─────────────────────────────────────────────────────────────
_ym_pat      = re.compile(r'^\d{4}-\d{2}$')
_strike_pat  = re.compile(r'BANKNIFTY (\d+) (CE|PE) (\d{2}) ([A-Z]{3}) (\d{2})')
_month_map   = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
                'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}


def build_opts_index():
    idx = {}
    for folder in sorted(OPTS_ROOT.iterdir()):
        if not _ym_pat.match(folder.name): continue
        strikes = set()
        exp_dates = set()
        for f in folder.glob('*.csv'):
            m = _strike_pat.search(f.name)
            if not m: continue
            strikes.add(int(m.group(1)))
            dd = int(m.group(3)); mon = _month_map[m.group(4)]; yy = int(m.group(5))+2000
            exp_dates.add(date(yy, mon, dd))
        if not strikes or not exp_dates: continue
        exp_date = min(exp_dates)
        ym_parts = folder.name.split('-')
        start_d  = date(int(ym_parts[0]), int(ym_parts[1]), 1)
        idx[folder.name] = {'path': folder, 'exp_date': exp_date,
                             'start_date': start_d, 'end_date': exp_date,
                             'strikes': sorted(strikes)}
    return idx


def find_opts_folder(idx, signal_date):
    # Pick the nearest upcoming expiry that hasn't expired yet on signal_date
    candidates = [v for v in idx.values() if v['exp_date'] >= signal_date]
    return min(candidates, key=lambda v: v['exp_date']) if candidates else None


def nearest_strike(strikes, price, direction=0, steps=0):
    """direction=0: ATM; direction=-1: CE ITM (below price); direction=+1: PE ITM (above price)"""
    atm = min(strikes, key=lambda s: abs(s - price))
    if direction == 0 or steps == 0:
        return atm
    atm_idx = strikes.index(atm)
    if direction == -1:   # CE ITM: go lower
        idx = max(0, atm_idx - steps)
    else:                 # PE ITM: go higher
        idx = min(len(strikes)-1, atm_idx + steps)
    return strikes[idx]


_opt_cache = {}
def load_opt_bars(path_str):
    if path_str in _opt_cache: return _opt_cache[path_str]
    bars = {}
    try:
        with open(path_str, newline='') as f:
            for r in csv.DictReader(f):
                if 'close' in r:
                    bars[(r['date'], r['time'][:5])] = float(r['close'])
    except Exception:
        pass
    _opt_cache[path_str] = bars
    return bars


def find_opt_file(folder, strike, opt_type, exp_date):
    # Try exact filename match first, then scan
    exp_str = exp_date.strftime('%d %b %y').upper()
    name = f"BANKNIFTY {strike} {opt_type} {exp_str}_5min.csv"
    p = folder / name
    if p.exists(): return p
    # Scan
    pat = re.compile(rf'BANKNIFTY {strike} {opt_type} \d{{2}} [A-Z]{{3}} \d{{2}}_5min\.csv')
    for f in folder.glob(f'BANKNIFTY {strike} {opt_type}*.csv'):
        if pat.match(f.name): return f
    return None


def get_opt_price(opt_bars, dt):
    d, t = dt.strftime('%Y-%m-%d'), dt.strftime('%H:%M')
    for delta in [0, 5, -5, 10, -10, 15, -15]:
        t2 = (dt + timedelta(minutes=delta)).strftime('%H:%M')
        v  = opt_bars.get((d, t2))
        if v is not None: return v
    return None


# ── Signal generators ─────────────────────────────────────────────────────────
def signals_baseline(bars):
    """Pure ST flip + ADX>=25"""
    dir_, atr_v = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    adx_v       = compute_adx(bars, ADX_PERIOD)
    signals = {}
    for i in range(max(ATR_PERIOD, ADX_PERIOD*2+1), len(bars)):
        d, dp = dir_[i], dir_[i-1]
        adx   = adx_v[i]; atr = atr_v[i]
        if math.isnan(adx) or math.isnan(atr): continue
        if adx >= 25 and d != dp:
            side = 'BUY' if d == -1 else 'SELL'
            signals[bars[i]['dt']] = {'side': side, 'atr': atr, 'fut_close': bars[i]['close']}
    return signals


def signals_pullback(bars):
    """Pullback-to-EMA after ST flip (ADX>=25)"""
    dir_, atr_v = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    adx_v       = compute_adx(bars, ADX_PERIOD)
    closes      = [b['close'] for b in bars]
    ema_v       = compute_ema(closes, 20)
    signals     = {}
    pending     = None
    WINDOW      = 6
    for i in range(max(ATR_PERIOD, ADX_PERIOD*2+1, 20), len(bars)):
        b   = bars[i]; c, h, l = b['close'], b['high'], b['low']
        d, dp = dir_[i], dir_[i-1]
        adx = adx_v[i]; atr = atr_v[i]; ema = ema_v[i]
        if math.isnan(adx) or math.isnan(atr) or math.isnan(ema): continue
        if adx >= 25 and d != dp and pending is None:
            pending = {'side': 'BUY' if d==-1 else 'SELL', 'atr': atr, 'bars_left': WINDOW}
        if pending:
            pending['bars_left'] -= 1
            side = pending['side']
            if side == 'BUY' and l <= ema and c > ema:
                signals[b['dt']] = {'side': side, 'atr': pending['atr'], 'fut_close': c}
                pending = None
            elif side == 'SELL' and h >= ema and c < ema:
                signals[b['dt']] = {'side': side, 'atr': pending['atr'], 'fut_close': c}
                pending = None
            elif pending['bars_left'] <= 0:
                pending = None
    return signals


# ── Core backtest ─────────────────────────────────────────────────────────────
def run_backtest(signal_fn_name, signals, bars5, opts_idx, itm_steps, label):
    """
    signals: {dt: {'side', 'atr', 'fut_close'}}  — on 15-min bars
    bars5  : 5-min bars for intraday exit tracking
    itm_steps: 0=ATM, 1=1 step ITM, 2=2 steps ITM
    """
    eod_set = last_bar_of_day(bars5)
    trades  = []
    active  = None   # current open trade

    # Build a 5-min bar lookup by dt
    bars5_by_dt = {b['dt']: b for b in bars5}

    # Process 5-min bars for exits; entries happen at signal dt
    pending_signals = dict(signals)   # copy

    for b in bars5:
        dt = b['dt']
        c, h, l = b['close'], b['high'], b['low']
        is_eod  = dt in eod_set

        # ── Check exit ──
        if active:
            sl, tp, side = active['sl_fut'], active['tp_fut'], active['side']
            opt_t = active['opt_type']
            hit_sl = (opt_t=='CE' and l <= sl) or (opt_t=='PE' and h >= sl)
            hit_tp = (opt_t=='CE' and h >= tp) or (opt_t=='PE' and l <= tp)
            if hit_sl or hit_tp or is_eod:
                exit_dt = dt
                # Get option premium at exit
                exit_px = get_opt_price(active['opt_bars'], exit_dt)
                if exit_px is None:
                    exit_px = active['entry_px'] * (0.3 if hit_sl else (2.0 if hit_tp else 0.85))
                exit_type = 'SL' if hit_sl else ('TP' if hit_tp else 'EOD')
                pnl_pts   = exit_px - active['entry_px']
                pnl_rs    = pnl_pts * LOT_SIZE
                trades.append({
                    'entry_dt':    active['entry_dt'],
                    'exit_dt':     exit_dt,
                    'side':        side,
                    'opt_type':    opt_t,
                    'strike':      active['strike'],
                    'entry_fut':   active['entry_fut'],
                    'entry_px':    active['entry_px'],
                    'exit_px':     exit_px,
                    'exit_type':   exit_type,
                    'pnl_rs':      pnl_rs,
                    'atr':         active['atr'],
                    'tte_days':    (active['exp_date'] - dt.date()).days,
                })
                active = None

        if active or is_eod:
            continue

        # ── Check entry (signal on this 15-min bucket or same dt) ──
        # signals are on 15-min dt; check if any pending signal matches this 5-min bar's 15-min bucket
        slot = dt.replace(minute=(dt.minute//15)*15)
        sig  = pending_signals.pop(slot, None)
        if sig is None:
            continue

        side    = sig['side']
        atr     = sig['atr']
        fut_c   = c   # use current 5-min close as proxy entry price

        # Find options folder
        info = find_opts_folder(opts_idx, dt.date())
        if info is None:
            continue
        strikes = info['strikes']

        # Strike selection
        direction = -1 if side == 'BUY' else +1   # CE ITM=below, PE ITM=above
        opt_type  = 'CE' if side == 'BUY' else 'PE'
        strike    = nearest_strike(strikes, fut_c, direction, itm_steps)

        # Load option data
        opt_file = find_opt_file(info['path'], strike, opt_type, info['exp_date'])
        if opt_file is None:
            continue
        opt_bars = load_opt_bars(str(opt_file))
        entry_px = get_opt_price(opt_bars, dt)
        if entry_px is None or entry_px < 10:
            continue

        # SL/TP in underlying terms
        if opt_type == 'CE':
            sl_fut = fut_c - SL_ATR * atr
            tp_fut = fut_c + TP_ATR * atr
        else:
            sl_fut = fut_c + SL_ATR * atr
            tp_fut = fut_c - TP_ATR * atr

        active = {
            'entry_dt':  dt,
            'side':      side,
            'opt_type':  opt_type,
            'strike':    strike,
            'entry_fut': fut_c,
            'entry_px':  entry_px,
            'opt_bars':  opt_bars,
            'sl_fut':    sl_fut,
            'tp_fut':    tp_fut,
            'atr':       atr,
            'exp_date':  info['exp_date'],
        }

    return trades


# ── Print helpers ─────────────────────────────────────────────────────────────
def print_summary(trades, label):
    if not trades:
        print(f"  {label}: no trades\n"); return
    wins  = [t for t in trades if t['pnl_rs'] > 0]
    total = sum(t['pnl_rs'] for t in trades)
    by_month = defaultdict(list)
    for t in trades:
        by_month[t['entry_dt'].strftime('%Y-%m')].append(t['pnl_rs'])

    print(f"\n  -- {label} --")
    print(f"  {'Month':<10}  {'Tr':>4}  {'W%':>5}  {'PnL (Rs)':>10}")
    print(f"  {'-'*40}")
    for ym in sorted(by_month):
        pts = by_month[ym]
        ww  = sum(1 for p in pts if p > 0)
        bar = ('+' * min(20, round(sum(pts)/2000))) if sum(pts)>0 else ('-' * min(20, round(-sum(pts)/2000)))
        print(f"  {ym:<10}  {len(pts):>4}  {ww/len(pts)*100:>4.0f}%  {sum(pts):>+10.0f}  {bar}")
    print(f"  {'TOTAL':<10}  {len(trades):>4}  {len(wins)/len(trades)*100:>4.0f}%  {total:>+10.0f}")
    tps = [t for t in trades if t['exit_type']=='TP']
    sls = [t for t in trades if t['exit_type']=='SL']
    eods= [t for t in trades if t['exit_type']=='EOD']
    if wins:  print(f"  Avg win : Rs {sum(t['pnl_rs'] for t in wins)/len(wins):>+.0f}")
    losses = [t for t in trades if t['pnl_rs'] <= 0]
    if losses: print(f"  Avg loss: Rs {sum(t['pnl_rs'] for t in losses)/len(losses):>+.0f}")
    print(f"  Exits   : TP={len(tps)}  SL={len(sls)}  EOD={len(eods)}")


def print_trade_table(trades, label):
    if not trades: return
    print(f"\n  Per-trade detail: {label}")
    print(f"  {'#':>3}  {'Entry':>16}  {'Side':<5}  {'Opt':<3}  {'Strike':>6}  "
          f"{'FutE':>6}  {'TTE':>4}  {'EntPx':>6}  {'ExtPx':>6}  {'Type':<5}  {'PnL Rs':>8}")
    print(f"  {'-'*90}")
    for n, t in enumerate(sorted(trades, key=lambda x: x['entry_dt']), 1):
        print(f"  {n:>3}  {t['entry_dt'].strftime('%Y-%m-%d %H:%M'):>16}  "
              f"{t['side']:<5}  {t['opt_type']:<3}  {t['strike']:>6}  "
              f"{t['entry_fut']:>6.0f}  {t['tte_days']:>4}d  "
              f"{t['entry_px']:>6.1f}  {t['exit_px']:>6.1f}  "
              f"{t['exit_type']:<5}  {t['pnl_rs']:>+8.0f}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Loading data...")
    bars5  = load_5min(FUTURES_CSV, START_6M, END_DATE)
    bars15 = to_15min(bars5)
    print(f"  5-min: {len(bars5):,}  |  15-min: {len(bars15):,}")

    print("Building options index...")
    opts_idx = build_opts_index()
    print(f"  {len(opts_idx)} expiry folders indexed")

    print("\nGenerating signals...")
    sigs_base = signals_baseline(bars15)
    sigs_pull = signals_pullback(bars15)
    print(f"  Baseline (ST flip)    : {len(sigs_base)} signals")
    print(f"  Pullback-to-EMA       : {len(sigs_pull)} signals")

    print(f"\n{'='*70}")
    print(f"  BUY OPTIONS BACKTEST  |  BANKNIFTY 15-min  |  6M")
    print(f"  SL = {SL_ATR}×ATR  |  TP = {TP_ATR}×ATR  |  Lot = {LOT_SIZE}")
    print(f"{'='*70}")

    results = {}
    configs = [
        ('Baseline + ATM',    sigs_base, 0),
        ('Baseline + ITM-1',  sigs_base, 1),
        ('Baseline + ITM-2',  sigs_base, 2),
        ('Pullback + ATM',    sigs_pull, 0),
        ('Pullback + ITM-1',  sigs_pull, 1),
        ('Pullback + ITM-2',  sigs_pull, 2),
    ]

    all_trades = {}
    for label, sigs, itm_steps in configs:
        trades = run_backtest(label, sigs, bars5, opts_idx, itm_steps, label)
        all_trades[label] = trades

    # ── Summary comparison table ──
    print(f"\n{'='*70}")
    print(f"  COMPARISON SUMMARY")
    print(f"  {'Config':<25}  {'Tr':>4}  {'Win%':>5}  {'PnL (Rs)':>10}  {'Avg/trade':>10}")
    print(f"  {'-'*62}")
    ref_pnl = 20677; ref_tr = 25
    print(f"  {'[Ref] Sell ATM (prev)':<25}  {ref_tr:>4}  {'36%':>5}  {ref_pnl:>+10}  "
          f"{ref_pnl/ref_tr:>+10.0f}")
    print(f"  {'-'*62}")
    for label, sigs, itm_steps in configs:
        trades = all_trades[label]
        if not trades:
            print(f"  {label:<25}  {'–':>4}  {'–':>5}  {'–':>10}  {'–':>10}"); continue
        wins  = sum(1 for t in trades if t['pnl_rs'] > 0)
        total = sum(t['pnl_rs'] for t in trades)
        print(f"  {label:<25}  {len(trades):>4}  {wins/len(trades)*100:>4.0f}%  "
              f"{total:>+10.0f}  {total/len(trades):>+10.0f}")

    # ── Month-wise trade tables for key configs ──
    key_configs = [
        ('Baseline + ATM',    'Buy ATM  | ST flip + ADX>=25'),
        ('Pullback + ITM-2',  'Buy ITM-2 | Pullback-to-EMA'),
    ]
    hdr = f"  {'#':>3}  {'Entry DT':<17} {'Sig':<5} {'Opt':<3} {'Strike':>6}  {'FutE':>6}  {'TTE':>4}  {'EntPx':>7}  {'ExtPx':>7}  {'Type':<5}  {'PnL Rs':>8}"
    sep = f"  {'-'*84}"

    for cfg_label, title in key_configs:
        trades = all_trades[cfg_label]
        print(f"\n{'='*88}")
        print(f"  MONTH-WISE TRADES: {title}")
        print(f"{'='*88}")

        by_month = defaultdict(list)
        for t in sorted(trades, key=lambda x: x['entry_dt']):
            by_month[t['entry_dt'].strftime('%Y-%m')].append(t)

        grand_pnl = 0
        grand_n   = 0
        for ym in sorted(by_month):
            month_trades = by_month[ym]
            month_pnl    = sum(t['pnl_rs'] for t in month_trades)
            month_wins   = sum(1 for t in month_trades if t['pnl_rs'] > 0)
            grand_pnl += month_pnl; grand_n += len(month_trades)
            print(f"\n  [{ym}]  {len(month_trades)} trades  {month_wins/len(month_trades)*100:.0f}% win  PnL: Rs {month_pnl:+,.0f}")
            print(hdr)
            print(sep)
            for n, t in enumerate(month_trades, 1):
                pnl_bar = ('+' if t['pnl_rs']>0 else '-') * min(8, int(abs(t['pnl_rs'])/500))
                print(f"  {n:>3}  {t['entry_dt'].strftime('%Y-%m-%d %H:%M'):<17} "
                      f"{t['side']:<5} {t['opt_type']:<3} {t['strike']:>6}  "
                      f"{t['entry_fut']:>6.0f}  {t['tte_days']:>4}d  "
                      f"{t['entry_px']:>7.1f}  {t['exit_px']:>7.1f}  "
                      f"{t['exit_type']:<5}  {t['pnl_rs']:>+8.0f}  {pnl_bar}")
        print(f"\n  TOTAL: {grand_n} trades  |  PnL: Rs {grand_pnl:+,.0f}")
    print()


if __name__ == '__main__':
    main()
