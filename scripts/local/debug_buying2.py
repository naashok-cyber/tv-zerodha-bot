"""Trace the full entry path for the first signal."""
import csv, math, re
from pathlib import Path
from datetime import date, datetime, timedelta

FUTURES_CSV = Path('data/banknifty_futures/banknifty_5min_futures.csv')
OPTS_ROOT   = Path('data/banknifty_options')
ATR_PERIOD, ST_MULT, ADX_PERIOD = 10, 3.0, 14
SL_ATR, TP_ATR = 1.0, 2.0
START_6M = date(2025, 11, 25); END_DATE = date(2026, 5, 26)
_ym_pat = re.compile(r'^\d{4}-\d{2}$')
_strike_pat = re.compile(r'BANKNIFTY (\d+) (CE|PE) (\d{2}) ([A-Z]{3}) (\d{2})')
_month_map = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
              'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}

def load_5min(path, start, end):
    bars = []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r['date'])
            if d < start or d > end: continue
            dt = datetime.fromisoformat(f"{r['date']}T{r['time']}")
            bars.append({'dt': dt, 'date': d, 'open': float(r['open']),
                         'high': float(r['high']), 'low': float(r['low']), 'close': float(r['close'])})
    return sorted(bars, key=lambda b: b['dt'])

def to_15min(bars5):
    buckets = {}
    for b in bars5:
        m = b['dt'].minute; slot = (m // 15) * 15
        key = b['dt'].replace(minute=slot, second=0)
        if key not in buckets:
            buckets[key] = {**b, 'dt': key}
        else:
            buckets[key]['high'] = max(buckets[key]['high'], b['high'])
            buckets[key]['low']  = min(buckets[key]['low'],  b['low'])
            buckets[key]['close'] = b['close']
    return sorted(buckets.values(), key=lambda b: b['dt'])

def wilder_atr(bars, period):
    n = len(bars); atr = [float('nan')] * n
    trs = [bars[0]['high']-bars[0]['low']] + [
        max(bars[i]['high']-bars[i]['low'],
            abs(bars[i]['high']-bars[i-1]['close']),
            abs(bars[i]['low'] -bars[i-1]['close'])) for i in range(1,n)]
    atr[period-1] = sum(trs[:period])/period
    for i in range(period, n): atr[i] = (atr[i-1]*(period-1)+trs[i])/period
    return atr

def compute_supertrend(bars, period, mult):
    n = len(bars); atr = wilder_atr(bars, period)
    ub=[float('nan')]*n; lb=[float('nan')]*n; dir_=[0]*n
    for i in range(period-1, n):
        hl2=(bars[i]['high']+bars[i]['low'])/2
        ub[i]=hl2+mult*atr[i]; lb[i]=hl2-mult*atr[i]
        if i==period-1: dir_[i]=1; continue
        ub[i]=min(ub[i],ub[i-1]) if bars[i-1]['close']<ub[i-1] else ub[i]
        lb[i]=max(lb[i],lb[i-1]) if bars[i-1]['close']>lb[i-1] else lb[i]
        if dir_[i-1]==1: dir_[i]=-1 if bars[i]['close']>ub[i-1] else 1
        else:            dir_[i]= 1 if bars[i]['close']<lb[i-1] else -1
    return dir_, atr

def compute_adx(bars, period=14):
    n=len(bars); adx=[float('nan')]*n
    if n<2*period+1: return adx
    pdm=[0.0]*n; ndm=[0.0]*n; tr=[0.0]*n
    for i in range(1,n):
        up=bars[i]['high']-bars[i-1]['high']; down=bars[i-1]['low']-bars[i]['low']
        pdm[i]=up if up>down and up>0 else 0.0
        ndm[i]=down if down>up and down>0 else 0.0
        tr[i]=max(bars[i]['high']-bars[i]['low'],abs(bars[i]['high']-bars[i-1]['close']),abs(bars[i]['low']-bars[i-1]['close']))
    alpha=1.0/period
    ap=sum(pdm[1:period+1]); an=sum(ndm[1:period+1]); at=sum(tr[1:period+1])
    dx_vals=[]
    for i in range(period+1,n):
        ap=ap*(1-alpha)+pdm[i]; an=an*(1-alpha)+ndm[i]; at=at*(1-alpha)+tr[i]
        pdi=100*ap/at if at else 0; ndi=100*an/at if at else 0
        denom=pdi+ndi
        dx_vals.append(100*abs(pdi-ndi)/denom if denom else 0)
        if len(dx_vals)>=period: adx[i]=sum(dx_vals[-period:])/period
    return adx

# Load data
bars5  = load_5min(FUTURES_CSV, START_6M, END_DATE)
bars15 = to_15min(bars5)
print(f"bars5={len(bars5)}  bars15={len(bars15)}")

# Generate signals
dir_, atr_v = compute_supertrend(bars15, ATR_PERIOD, ST_MULT)
adx_v       = compute_adx(bars15, ADX_PERIOD)
signals = {}
for i in range(max(ATR_PERIOD, ADX_PERIOD*2+1), len(bars15)):
    d, dp = dir_[i], dir_[i-1]
    adx = adx_v[i]; atr = atr_v[i]
    if math.isnan(adx) or math.isnan(atr): continue
    if adx >= 25 and d != dp:
        side = 'BUY' if d == -1 else 'SELL'
        signals[bars15[i]['dt']] = {'side': side, 'atr': atr, 'fut_close': bars15[i]['close']}
print(f"Signals: {len(signals)}")

# First signal
first_dt = sorted(signals.keys())[0]
sig = signals[first_dt]
print(f"\nFirst signal: {first_dt}  side={sig['side']}  atr={sig['atr']:.1f}  fut={sig['fut_close']:.0f}")

# EOD check
eod_dict = {}
for b in bars5: eod_dict[b['date']] = b['dt']
eod_set = set(eod_dict.values())
print(f"Is signal dt in EOD set? {first_dt in eod_set}")

# Options index
idx = {}
for folder in sorted(OPTS_ROOT.iterdir()):
    if not _ym_pat.match(folder.name): continue
    strikes = set(); exp_dates = set()
    for f in folder.glob('*.csv'):
        m = _strike_pat.search(f.name)
        if not m: continue
        strikes.add(int(m.group(1)))
        dd=int(m.group(3)); mon=_month_map[m.group(4)]; yy=int(m.group(5))+2000
        exp_dates.add(date(yy,mon,dd))
    if not strikes or not exp_dates: continue
    exp_date=min(exp_dates)
    ym_parts=folder.name.split('-')
    start_d=date(int(ym_parts[0]),int(ym_parts[1]),1)
    idx[folder.name]={'path':folder,'exp_date':exp_date,'start_date':start_d,'end_date':exp_date,'strikes':sorted(strikes)}

signal_date = first_dt.date()
candidates = [v for v in idx.values() if v['exp_date'] >= signal_date]
print(f"\nCandidates for {signal_date}: {[(k, v['exp_date']) for k,v in idx.items() if v['exp_date'] >= signal_date][:5]}")
info = min(candidates, key=lambda v: v['exp_date']) if candidates else None
if info:
    print(f"Chosen folder exp={info['exp_date']}  strikes range={info['strikes'][0]}..{info['strikes'][-1]}")
    fut_c = sig['fut_close']
    opt_type = 'CE' if sig['side']=='BUY' else 'PE'
    direction = -1 if sig['side']=='BUY' else +1
    atm = min(info['strikes'], key=lambda s: abs(s-fut_c))
    atm_idx = info['strikes'].index(atm)
    steps=1
    if direction==-1: itm_idx=max(0, atm_idx-steps)
    else:             itm_idx=min(len(info['strikes'])-1, atm_idx+steps)
    strike_itm = info['strikes'][itm_idx]
    print(f"fut={fut_c:.0f}  opt_type={opt_type}  ATM={atm}  ITM-1 strike={strike_itm}")

    # Look for file
    exp_str = info['exp_date'].strftime('%d %b %y').upper()
    print(f"Exp string: '{exp_str}'")
    name = f"BANKNIFTY {atm} {opt_type} {exp_str}_5min.csv"
    p = info['path'] / name
    print(f"Looking for: {p}")
    print(f"Exists: {p.exists()}")

    # List actual files to see format
    sample_files = list(info['path'].glob(f'BANKNIFTY {atm}*.csv'))[:3]
    print(f"Actual files for {atm}: {[f.name for f in sample_files]}")

    # Try all PE files near ATM
    for sk in [atm-100, atm, atm+100]:
        files = list(info['path'].glob(f'BANKNIFTY {sk} {opt_type}*.csv'))
        if files: print(f"  Found {opt_type} files for {sk}: {files[0].name}")
else:
    print("No info found!")

# Check pending_signals dict handling
print("\n--- Testing signal slot matching in 5-min loop ---")
pending = {first_dt: sig}
matched = False
for b in bars5:
    dt = b['dt']
    slot = dt.replace(minute=(dt.minute//15)*15)
    if slot in pending:
        print(f"Match! 5-min bar {dt} -> slot {slot} == signal {first_dt}")
        matched = True
        break
if not matched:
    print("Never matched in bars5!")
