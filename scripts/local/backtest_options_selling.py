"""
Options selling backtest: SELL ATM PE on BUY signal, SELL ATM CE on SELL signal.
Signals: SuperTrend flip + ADX>=25 (best config from futures backtest).
SL: underlying moves ATR*1.5 against position.
TP: 1:2 RR = underlying moves ATR*3.0 in favour.
SL/TP checked on 5-min futures bars; option premium read at the crossing bar.
PnL = (entry_premium - exit_premium) * LOT_SIZE per trade.

Timeframes: 5-min signals, 15-min signals (both tracked via 5-min data).
Period: last 6 months from May 26 2026 (data end).
"""
import csv, re, math
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict
from math import log, sqrt, exp
from scipy.stats import norm as _norm

# ── Parameters ────────────────────────────────────────────────────────────────
ATR_PERIOD  = 10
ST_MULT     = 3.0
ADX_PERIOD  = 14
ADX_THRESH  = 25.0
SL_ATR      = 1.5
TP_ATR      = 3.0          # 1:2 RR (risk ATR*1.5 to make ATR*3.0)
LOT_SIZE    = 15           # BN lot

DATA_END    = date(2026, 5, 26)
START_6M    = DATA_END - timedelta(days=182)

OPTS_DIR    = Path('data/banknifty_options')
RISK_FREE   = 0.07   # 7% p.a.


# ── IV calculation (BSM, Newton-Raphson) ──────────────────────────────────────

def _bs_price(S, K, T, r, sigma, otype):
    if T <= 1e-6 or sigma <= 1e-6:
        return max(0.0, S - K) if otype == 'CE' else max(0.0, K - S)
    d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if otype == 'CE':
        return S * _norm.cdf(d1) - K * exp(-r * T) * _norm.cdf(d2)
    else:
        return K * exp(-r * T) * _norm.cdf(-d2) - S * _norm.cdf(-d1)


def calc_iv(S: float, K: float, T: float, r: float,
            market_price: float, otype: str) -> float:
    """Returns IV as a decimal (0.25 = 25%). Returns nan if unsolvable."""
    if T <= 1e-6 or market_price <= 0 or S <= 0:
        return float('nan')
    intrinsic = max(0.0, S - K) if otype == 'CE' else max(0.0, K - S)
    if market_price <= intrinsic:
        return float('nan')
    sigma = 0.30   # starting guess 30%
    for _ in range(200):
        try:
            price = _bs_price(S, K, T, r, sigma, otype)
            d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
            vega = S * _norm.cdf(d1) * sqrt(T)
            if abs(vega) < 1e-12:
                break
            sigma -= (price - market_price) / vega
            sigma = max(sigma, 1e-4)
            if abs(price - market_price) < 0.001:
                break
        except Exception:
            return float('nan')
    return sigma if 0.01 < sigma < 30.0 else float('nan')
FUT_FILE    = Path('data/banknifty_futures/banknifty_5min_futures.csv')

# ── Options folder → expiry string map ────────────────────────────────────────

def build_opts_index() -> dict:
    """Returns {ym: {'path':Path, 'exp_str':str, 'exp_date':date, 'strikes':sorted list}}"""
    idx = {}
    for d in sorted(OPTS_DIR.iterdir()):
        if not d.is_dir() or not re.match(r'^\d{4}-\d{2}$', d.name):
            continue
        pe_files = list(d.glob('BANKNIFTY*PE*5min.csv'))
        if not pe_files:
            continue
        m = re.search(r'(\d{2}) ([A-Z]{3}) (\d{2})', pe_files[0].name)
        if not m:
            continue
        day, mon_str, yr2 = m.group(1), m.group(2), m.group(3)
        mon_map = dict(JAN=1,FEB=2,MAR=3,APR=4,MAY=5,JUN=6,JUL=7,AUG=8,SEP=9,OCT=10,NOV=11,DEC=12)
        exp_date = date(2000+int(yr2), mon_map[mon_str], int(day))
        # get date range of data in this folder
        rows = list(csv.DictReader(open(pe_files[0], newline='', encoding='utf-8')))
        if not rows:
            continue
        dates = [r['date'] for r in rows]
        # available strikes
        strikes = sorted(set(
            int(re.search(r'BANKNIFTY (\d+)', f.name).group(1))
            for f in pe_files
        ))
        idx[d.name] = {
            'path': d,
            'exp_str': f'{day} {mon_str} {yr2}',
            'exp_date': exp_date,
            'start_date': date.fromisoformat(min(dates)),
            'end_date':   date.fromisoformat(max(dates)),
            'strikes': strikes,
        }
    return idx


def find_opts_folder(idx: dict, signal_date: date) -> dict | None:
    """Find the best options folder for a given signal date (nearest monthly expiry)."""
    candidates = []
    for ym, info in idx.items():
        if info['start_date'] <= signal_date <= info['end_date'] and info['exp_date'] >= signal_date:
            candidates.append(info)
    if not candidates:
        return None
    # Pick the folder whose expiry is soonest (current month's options)
    return min(candidates, key=lambda x: x['exp_date'])


def nearest_strike(strikes: list, price: float) -> int:
    return min(strikes, key=lambda s: abs(s - price))


_opt_cache: dict[Path, dict] = {}

def load_opt_bars(path: Path) -> dict:
    """Load option 5-min bars → {(date_str, time_str): close}"""
    if path in _opt_cache:
        return _opt_cache[path]
    result = {}
    if path.exists():
        with open(path, newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                t = r['time'][:5]  # HH:MM
                result[(r['date'], t)] = float(r['close'])
    _opt_cache[path] = result
    return result


def get_opt_price(opt_bars: dict, dt: datetime) -> float | None:
    key = (dt.strftime('%Y-%m-%d'), dt.strftime('%H:%M'))
    v = opt_bars.get(key)
    if v is not None:
        return v
    # Try nearby minutes (options data may skip some bars)
    for delta in [5, -5, 10, -10, 15, -15]:
        from datetime import timedelta as td
        alt = dt + td(minutes=delta)
        key2 = (alt.strftime('%Y-%m-%d'), alt.strftime('%H:%M'))
        v = opt_bars.get(key2)
        if v is not None:
            return v
    return None


# ── Indicator functions (same as futures backtest) ─────────────────────────────

def wilder_atr(bars, period):
    n = len(bars)
    atr = [float('nan')] * n
    running = bars[0]['high'] - bars[0]['low']
    for i in range(1, n):
        h, l, pc = bars[i]['high'], bars[i]['low'], bars[i-1]['close']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if i < period:
            running += tr
            if i == period - 1:
                atr[i] = running / period
        else:
            atr[i] = (atr[i-1] * (period - 1) + tr) / period
    return atr


def compute_supertrend(bars, period, mult):
    n = len(bars)
    atr_v = wilder_atr(bars, period)
    direction = [0] * n
    upper = [float('nan')] * n
    lower = [float('nan')] * n
    for i in range(n):
        a = atr_v[i]
        if math.isnan(a):
            continue
        hl2 = (bars[i]['high'] + bars[i]['low']) / 2
        upper[i] = hl2 + mult * a
        lower[i] = hl2 - mult * a
        if i == 0 or direction[i-1] == 0:
            direction[i] = -1
            continue
        if not math.isnan(upper[i-1]):
            upper[i] = min(upper[i], upper[i-1]) if bars[i-1]['close'] < upper[i-1] else upper[i]
        if not math.isnan(lower[i-1]):
            lower[i] = max(lower[i], lower[i-1]) if bars[i-1]['close'] > lower[i-1] else lower[i]
        prev_dir = direction[i-1]
        c = bars[i]['close']
        if prev_dir == 1:
            direction[i] = -1 if c > upper[i] else 1
        else:
            direction[i] = 1 if c < lower[i] else -1
    return direction


def compute_adx(bars, period=14):
    n = len(bars)
    adx = [float('nan')] * n
    alpha = 1.0 / period
    tr_v, pdm_v, ndm_v = [], [], []
    for i in range(n):
        h, l = bars[i]['high'], bars[i]['low']
        if i == 0:
            tr_v.append(h - l); pdm_v.append(0.0); ndm_v.append(0.0)
            continue
        pc = bars[i-1]['close']
        ph, pl = bars[i-1]['high'], bars[i-1]['low']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        pdm_v.append(up   if up > dn and up > 0   else 0.0)
        ndm_v.append(dn   if dn > up and dn > 0   else 0.0)
        tr_v.append(tr)
    s_tr = s_pdm = s_ndm = float('nan')
    dx_v = [float('nan')] * n
    for i in range(n):
        if i < period - 1:
            continue
        elif i == period - 1:
            s_tr = sum(tr_v[:period]); s_pdm = sum(pdm_v[:period]); s_ndm = sum(ndm_v[:period])
        else:
            s_tr  = s_tr  - s_tr  * alpha + tr_v[i]
            s_pdm = s_pdm - s_pdm * alpha + pdm_v[i]
            s_ndm = s_ndm - s_ndm * alpha + ndm_v[i]
        if s_tr and s_tr > 0:
            pdi = 100 * s_pdm / s_tr
            ndi = 100 * s_ndm / s_tr
            denom = pdi + ndi
            dx_v[i] = 100 * abs(pdi - ndi) / denom if denom else 0.0
    adx_val = float('nan')
    dx_count = dx_sum = 0.0
    for i in range(n):
        dx = dx_v[i]
        if math.isnan(dx):
            continue
        if math.isnan(adx_val):
            dx_sum += dx; dx_count += 1
            if dx_count == period:
                adx_val = dx_sum / period
                adx[i] = adx_val
        else:
            adx_val = adx_val * (1 - alpha) + dx * alpha
            adx[i] = adx_val
    return adx


# ── Data loading ───────────────────────────────────────────────────────────────

def load_5min(path: Path) -> list[dict]:
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append({
                'dt':    datetime.fromisoformat(f"{r['date']} {r['time']}"),
                'open':  float(r['open']),
                'high':  float(r['high']),
                'low':   float(r['low']),
                'close': float(r['close']),
            })
    rows.sort(key=lambda x: x['dt'])
    return rows


def resample_15min(bars5: list[dict]) -> list[dict]:
    bucket = {}
    for b in bars5:
        m = b['dt'].minute
        key = b['dt'].replace(minute=(m // 15) * 15, second=0, microsecond=0)
        if key not in bucket:
            bucket[key] = {**b, 'dt': key}
        else:
            bk = bucket[key]
            bk['high']  = max(bk['high'],  b['high'])
            bk['low']   = min(bk['low'],   b['low'])
            bk['close'] = b['close']
    return sorted(bucket.values(), key=lambda x: x['dt'])


# ── Main backtest ──────────────────────────────────────────────────────────────

def generate_signals(bars: list[dict]) -> dict:
    """Returns {dt: {'side': 'BUY'/'SELL', 'atr': float, 'fut_close': float}}"""
    n = len(bars)
    dir_v = compute_supertrend(bars, ATR_PERIOD, ST_MULT)
    atr_v = wilder_atr(bars, ATR_PERIOD)
    adx_v = compute_adx(bars, ADX_PERIOD)
    signals = {}
    for i in range(1, n):
        d, dp = dir_v[i], dir_v[i-1]
        atr, adx = atr_v[i], adx_v[i]
        if d == 0 or math.isnan(atr) or math.isnan(adx):
            continue
        if adx >= ADX_THRESH and d != dp:
            side = 'BUY' if d == -1 else 'SELL'
            signals[bars[i]['dt']] = {'side': side, 'atr': atr, 'fut_close': bars[i]['close']}
    return signals


def _build_eod_set(bars5):
    """Last 5-min bar datetime for each trading day."""
    last = {}
    for b in bars5:
        last[b['dt'].date()] = b['dt']
    return set(last.values())


def backtest_options(bars5: list[dict], signal_bars: list[dict], opts_idx: dict, start: date,
                     eod_exit: bool = False) -> list[dict]:
    """
    bars5      : 5-min futures bars (for SL/TP tracking)
    signal_bars: 5-min or 15-min bars (for signal generation)
    eod_exit   : if True, close any open trade at end of each day
    """
    signals  = generate_signals(signal_bars)
    eod_set  = _build_eod_set(bars5) if eod_exit else set()

    trades  = []
    active  = None   # open trade

    for i, b in enumerate(bars5):
        if b['dt'].date() < start:
            continue

        dt = b['dt']
        h, l, c = b['high'], b['low'], b['close']
        is_eod   = dt in eod_set

        # ── Check SL / TP on active trade ─────────────────────────────────────
        if active:
            opt_t = active['opt_type']
            sl    = active['sl_fut']
            tp    = active['tp_fut']

            # Short CE: SL if underlying rises above sl, TP if it falls below tp
            # Short PE: SL if underlying falls below sl, TP if it rises above tp
            hit_sl = (opt_t == 'CE' and h >= sl) or (opt_t == 'PE' and l <= sl)
            hit_tp = (opt_t == 'CE' and l <= tp) or (opt_t == 'PE' and h >= tp)

            if hit_sl and hit_tp:
                exit_type = 'SL'
            elif hit_sl:
                exit_type = 'SL'
            elif hit_tp:
                exit_type = 'TP'
            elif is_eod:
                exit_type = 'EOD'
            else:
                exit_type = None

            if exit_type:
                exit_premium = get_opt_price(active['opt_bars'], dt)
                if exit_premium is not None:
                    pnl_pt  = active['entry_premium'] - exit_premium
                    pnl_rs  = pnl_pt * LOT_SIZE
                    trades.append({
                        'entry_dt':       active['entry_dt'],
                        'exit_dt':        dt,
                        'side':           active['side'],
                        'opt_type':       active['opt_type'],
                        'atm':            active['atm'],
                        'entry_fut':      active['entry_fut'],
                        'entry_premium':  active['entry_premium'],
                        'entry_iv':       active['entry_iv'],
                        'T_years':        active['T_years'],
                        'exit_premium':   exit_premium,
                        'exit_type':      exit_type,
                        'pnl_pts':        round(pnl_pt, 2),
                        'pnl_rs':         round(pnl_rs, 2),
                    })
                active = None

        # ── No new entries on EOD bar ──────────────────────────────────────────
        if is_eod:
            continue

        # ── Check for new signal ───────────────────────────────────────────────
        if active is None and dt in signals:
            sig     = signals[dt]
            side    = sig['side']
            atr     = sig['atr']
            fut_c   = sig['fut_close']
            opt_type = 'CE' if side == 'BUY' else 'PE'

            # Find the right options folder and nearest strike
            info = find_opts_folder(opts_idx, dt.date())
            if info is None:
                continue

            atm = nearest_strike(info['strikes'], fut_c)

            # Build option filename
            fname  = f'BANKNIFTY {atm} {opt_type} {info["exp_str"]}_5min.csv'
            fpath  = info['path'] / fname
            opt_bars = load_opt_bars(fpath)

            entry_premium = get_opt_price(opt_bars, dt)
            if entry_premium is None or entry_premium <= 0:
                continue

            # Sell CE on BUY: SL if price rises (CE moves ITM), TP if price drops (CE OTM)
            # Sell PE on SELL: SL if price drops (PE moves ITM), TP if price rises (PE OTM)
            sl_fut = fut_c + SL_ATR * atr if opt_type == 'CE' else fut_c - SL_ATR * atr
            tp_fut = fut_c - TP_ATR * atr if opt_type == 'CE' else fut_c + TP_ATR * atr

            # Calculate IV at entry
            T_years = (info['exp_date'] - dt.date()).days / 365.0
            iv = calc_iv(fut_c, atm, T_years, RISK_FREE, entry_premium, opt_type)

            active = {
                'side':          side,
                'opt_type':      opt_type,
                'atm':           atm,
                'entry_dt':      dt,
                'entry_fut':     fut_c,
                'entry_premium': entry_premium,
                'entry_iv':      iv,
                'T_years':       T_years,
                'sl_fut':        sl_fut,
                'tp_fut':        tp_fut,
                'atr':           atr,
                'opt_bars':      opt_bars,
            }

    # Close open trade at last bar
    if active:
        b = bars5[-1]
        ep = get_opt_price(active['opt_bars'], b['dt'])
        if ep:
            pnl_pt = active['entry_premium'] - ep
            trades.append({
                'entry_dt':      active['entry_dt'],
                'exit_dt':       b['dt'],
                'side':          active['side'],
                'opt_type':      active['opt_type'],
                'atm':           active['atm'],
                'entry_premium': active['entry_premium'],
                'exit_premium':  ep,
                'exit_type':     'OPEN',
                'pnl_pts':       round(pnl_pt, 2),
                'pnl_rs':        round(pnl_pt * LOT_SIZE, 2),
            })

    return trades


def print_monthly(trades: list[dict], label: str):
    monthly_pnl = defaultdict(float)
    monthly_tr  = defaultdict(int)
    monthly_w   = defaultdict(int)
    for t in trades:
        ym = t['entry_dt'].strftime('%Y-%m')
        monthly_pnl[ym] += t['pnl_rs']
        monthly_tr[ym]  += 1
        if t['pnl_rs'] > 0:
            monthly_w[ym] += 1

    total_pnl = sum(t['pnl_rs'] for t in trades)
    wins       = [t for t in trades if t['pnl_rs'] > 0]
    losses     = [t for t in trades if t['pnl_rs'] <= 0]
    wr         = 100 * len(wins) / len(trades) if trades else 0

    print(f'\n  {label}  ({len(trades)} trades, {wr:.0f}% win rate)')
    print(f'  {"Month":<10} {"Tr":>3}  {"W%":>4}  {"PnL (Rs)":>10}')
    print(f'  {"-"*38}')
    for ym in sorted(monthly_pnl):
        tr = monthly_tr[ym]; w = monthly_w[ym]
        wp = int(100*w/tr) if tr else 0
        pnl = monthly_pnl[ym]
        bar = ('+' if pnl>0 else '-') * min(int(abs(pnl)/2000), 25)
        print(f'  {ym:<10} {tr:>3}  {wp:>3}%  {pnl:>+10,.0f}  {bar}')
    print(f'  {"TOTAL":<10} {len(trades):>3}  {wr:>3.0f}%  {total_pnl:>+10,.0f}')
    if wins:
        print(f'  Avg win: Rs {sum(t["pnl_rs"] for t in wins)/len(wins):+,.0f}  '
              f'Avg loss: Rs {sum(t["pnl_rs"] for t in losses)/len(losses):+,.0f}')


def main():
    print('Loading futures data...')
    bars5  = load_5min(FUT_FILE)
    bars15 = resample_15min(bars5)
    print(f'  5-min: {len(bars5):,} bars  |  15-min: {len(bars15):,} bars')
    print(f'  Range: {bars5[0]["dt"].date()} to {bars5[-1]["dt"].date()}')

    print('\nBuilding options index...')
    opts_idx = build_opts_index()
    print(f'  {len(opts_idx)} expiry folders indexed')

    print(f'\nRunning options backtest (ADX>={ADX_THRESH}, SL=ATR*{SL_ATR}, TP=ATR*{TP_ATR}, 1:{int(TP_ATR/SL_ATR)} RR)')
    print(f'Period: last 6M from {START_6M} to {DATA_END}')
    print(f'  Sell ATM CE on BUY signal, Sell ATM PE on SELL signal')
    print(f'  Lot size: {LOT_SIZE}  |  PnL in Rs per lot\n')

    print('=' * 55)
    print('  BANKNIFTY  —  OPTIONS SELLING')
    print('=' * 55)

    trades_5m      = backtest_options(bars5, bars5,  opts_idx, START_6M, eod_exit=False)
    trades_15m     = backtest_options(bars5, bars15, opts_idx, START_6M, eod_exit=False)
    trades_5m_eod  = backtest_options(bars5, bars5,  opts_idx, START_6M, eod_exit=True)
    trades_15m_eod = backtest_options(bars5, bars15, opts_idx, START_6M, eod_exit=True)

    print_monthly(trades_5m,  '[5-min  | Overnight] Sell ATM CE/PE')
    print_monthly(trades_15m, '[15-min | Overnight] Sell ATM CE/PE')
    print_monthly(trades_5m_eod,  '[5-min  | EOD Exit ] Sell ATM CE/PE')
    print_monthly(trades_15m_eod, '[15-min | EOD Exit ] Sell ATM CE/PE')

    def print_trades_by_month(trades, label):
        print('\n\n' + '=' * 105)
        print(f'  {label}')
        print('=' * 105)
        hdr = (f'  {"#":>3}  {"Entry DT":<17}  {"Exit DT":<17}  {"Sig":<5} {"Opt":<3} '
               f'{"ATM":>6}  {"Fut":>7}  {"TTE":>4}  {"IV%":>5}  '
               f'{"EntPx":>7}  {"ExtPx":>7}  {"Type":<5}  {"PnL Rs":>9}')
        sep = '  ' + '-' * 101

        by_month = defaultdict(list)
        for t in trades:
            by_month[t['entry_dt'].strftime('%Y-%m')].append(t)

        grand_pnl = 0
        n_total   = 0
        for ym in sorted(by_month):
            month_trades = by_month[ym]
            month_pnl    = sum(t['pnl_rs'] for t in month_trades)
            month_wins   = sum(1 for t in month_trades if t['pnl_rs'] > 0)
            grand_pnl   += month_pnl
            n_total     += len(month_trades)
            bar = ('+' if month_pnl > 0 else '-') * min(12, int(abs(month_pnl) / 2000))
            print(f'\n  [{ym}]  {len(month_trades)} trades  {month_wins/len(month_trades)*100:.0f}% win  '
                  f'PnL: Rs {month_pnl:+,.0f}  {bar}')
            print(hdr)
            print(sep)
            for idx, t in enumerate(month_trades, 1):
                tte  = round(t.get('T_years', 0) * 365)
                iv   = t.get('entry_iv', float('nan'))
                iv_s = f"{iv*100:.1f}%" if not math.isnan(iv) else ' N/A '
                pbar = ('+' if t['pnl_rs'] > 0 else '-') * min(6, int(abs(t['pnl_rs']) / 1000))
                exit_s = t['exit_dt'].strftime('%Y-%m-%d %H:%M') if 'exit_dt' in t else '—'
                print(f'  {n_total - len(month_trades) + idx:>3}  '
                      f'{t["entry_dt"].strftime("%Y-%m-%d %H:%M"):<17}  '
                      f'{exit_s:<17}  '
                      f'{t["side"]:<5} {t["opt_type"]:<3} '
                      f'{t["atm"]:>6}  {t.get("entry_fut", 0):>7.0f}  '
                      f'{tte:>4}d  {iv_s:>5}  '
                      f'{t["entry_premium"]:>7.1f}  {t["exit_premium"]:>7.1f}  '
                      f'{t["exit_type"]:<5}  {t["pnl_rs"]:>+9.0f}  {pbar}')

        wins = sum(1 for t in trades if t['pnl_rs'] > 0)
        losses = [t for t in trades if t['pnl_rs'] <= 0]
        w_trades = [t for t in trades if t['pnl_rs'] > 0]
        tps = sum(1 for t in trades if t['exit_type'] == 'TP')
        sls = sum(1 for t in trades if t['exit_type'] == 'SL')
        print(f'\n  {"="*55}')
        print(f'  TOTAL: {n_total} trades  |  Win: {wins} ({wins/n_total*100:.0f}%)  '
              f'|  TP: {tps}  SL: {sls}  |  PnL: Rs {grand_pnl:+,.0f}')
        if w_trades:
            print(f'  Avg win : Rs {sum(t["pnl_rs"] for t in w_trades)/len(w_trades):+,.0f}')
        if losses:
            print(f'  Avg loss: Rs {sum(t["pnl_rs"] for t in losses)/len(losses):+,.0f}')

    print_trades_by_month(trades_15m,     '15-MIN | OVERNIGHT — all trades')
    print_trades_by_month(trades_15m_eod, '15-MIN | EOD EXIT  — all trades')

    def stats(tlist):
        if not tlist: return 'N/A', 0, 0, 0, 0, 0
        w   = sum(1 for t in tlist if t['pnl_rs'] > 0)
        sl  = sum(1 for t in tlist if t['exit_type'] == 'SL')
        tp  = sum(1 for t in tlist if t['exit_type'] == 'TP')
        eod = sum(1 for t in tlist if t['exit_type'] == 'EOD')
        pnl = sum(t['pnl_rs'] for t in tlist)
        return f'Rs {pnl:+,.0f}', len(tlist), int(100*w/len(tlist)), tp, sl, eod

    print('\n\n' + '=' * 68)
    print(f'  {"":25} {"5m Ovnt":>10}  {"5m EOD":>10}  {"15m Ovnt":>10}  {"15m EOD":>10}')
    print(f'  {"-"*65}')
    p5,  n5,  w5,  tp5,  sl5,  e5   = stats(trades_5m)
    p5e, n5e, w5e, tp5e, sl5e, e5e  = stats(trades_5m_eod)
    p15, n15, w15, tp15, sl15, e15  = stats(trades_15m)
    p15e,n15e,w15e,tp15e,sl15e,e15e = stats(trades_15m_eod)
    print(f'  {"Total PnL":<25} {p5:>10}  {p5e:>10}  {p15:>10}  {p15e:>10}')
    print(f'  {"Trades":<25} {n5:>10}  {n5e:>10}  {n15:>10}  {n15e:>10}')
    print(f'  {"Win rate":<25} {str(w5)+"%":>10}  {str(w5e)+"%":>10}  {str(w15)+"%":>10}  {str(w15e)+"%":>10}')
    print(f'  {"TP hits":<25} {tp5:>10}  {tp5e:>10}  {tp15:>10}  {tp15e:>10}')
    print(f'  {"SL hits":<25} {sl5:>10}  {sl5e:>10}  {sl15:>10}  {sl15e:>10}')
    print(f'  {"EOD exits":<25} {e5:>10}  {e5e:>10}  {e15:>10}  {e15e:>10}')


if __name__ == '__main__':
    main()
