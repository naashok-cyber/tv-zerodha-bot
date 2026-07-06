import csv, math, re
from pathlib import Path
from datetime import date, datetime, timedelta

FUTURES_CSV = Path('data/banknifty_futures/banknifty_5min_futures.csv')
OPTS_ROOT   = Path('data/banknifty_options')
START_6M    = date(2025, 11, 25)
END_DATE    = date(2026, 5, 26)

# Load 5-min bars
bars = []
with open(FUTURES_CSV, newline='') as f:
    for r in csv.DictReader(f):
        d = date.fromisoformat(r['date'])
        if d < START_6M or d > END_DATE: continue
        dt = datetime.fromisoformat(f"{r['date']}T{r['time']}")
        bars.append({'dt': dt, 'date': d, 'close': float(r['close'])})
bars.sort(key=lambda b: b['dt'])
print(f"5-min bars: {len(bars)}")
print(f"First 3: {[b['dt'] for b in bars[:3]]}")

# Bucket to 15-min
buckets = {}
for b in bars:
    m = b['dt'].minute; slot = (m // 15) * 15
    key = b['dt'].replace(minute=slot, second=0)
    if key not in buckets:
        buckets[key] = {'dt': key, 'date': b['date'], 'close': b['close']}
    else:
        buckets[key]['close'] = b['close']
bars15 = sorted(buckets.values(), key=lambda b: b['dt'])
print(f"15-min bars: {len(bars15)}")
print(f"First 3: {[b['dt'] for b in bars15[:3]]}")

# Check: a known signal from previous run was at 2025-11-27 13:15
sig_dt = datetime(2025, 11, 27, 13, 15)
print(f"\nLooking for signal dt {sig_dt} in 5-min bars...")
found = False
for b in bars:
    slot = b['dt'].replace(minute=(b['dt'].minute // 15) * 15)
    if slot == sig_dt:
        print(f"  Match: 5-min bar {b['dt']} -> slot {slot}")
        found = True
        break
if not found:
    print("  NOT FOUND - checking first 20 bars slots:")
    for b in bars[:20]:
        slot = b['dt'].replace(minute=(b['dt'].minute // 15) * 15)
        print(f"  bar={b['dt']}  slot={slot}")

# Check if sig_dt exists in bars15
exists_in_15 = any(b['dt'] == sig_dt for b in bars15)
print(f"\nDoes {sig_dt} exist in bars15? {exists_in_15}")

# Check opts index
_ym_pat = re.compile(r'^\d{4}-\d{2}$')
_strike_pat = re.compile(r'BANKNIFTY (\d+) (CE|PE) (\d{2}) ([A-Z]{3}) (\d{2})')
_month_map = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
              'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}

idx = {}
for folder in sorted(OPTS_ROOT.iterdir()):
    if not _ym_pat.match(folder.name): continue
    strikes = set(); exp_dates = set()
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
                         'start_date': start_d, 'end_date': exp_date, 'strikes': sorted(strikes)}

print(f"\nOpts index months: {list(idx.keys())}")
signal_date = date(2025, 11, 27)
cands = [v for v in idx.values()
         if v['start_date'] <= signal_date <= v['end_date']
         and v['exp_date'] >= signal_date]
print(f"Candidates for {signal_date}: {[v['exp_date'] for v in cands]}")
if cands:
    info = min(cands, key=lambda v: v['exp_date'])
    print(f"  Chosen: exp={info['exp_date']}  strikes[50:55]={info['strikes'][50:55]}")
    # Try finding a PE file near 59800
    fut_price = 59772
    atm = min(info['strikes'], key=lambda s: abs(s - fut_price))
    itm_pe = info['strikes'][info['strikes'].index(atm) + 1] if info['strikes'].index(atm) < len(info['strikes'])-1 else atm
    print(f"  ATM={atm}  ITM-PE={itm_pe}")
    exp_str = info['exp_date'].strftime('%d %b %y').upper()
    name_pe = f"BANKNIFTY {atm} PE {exp_str}_5min.csv"
    p = info['path'] / name_pe
    print(f"  Looking for: {p}")
    print(f"  Exists: {p.exists()}")
    # Try listing PE files near atm
    pe_files = list(info['path'].glob(f'BANKNIFTY {atm} PE*.csv'))
    print(f"  PE files for {atm}: {[f.name for f in pe_files]}")
