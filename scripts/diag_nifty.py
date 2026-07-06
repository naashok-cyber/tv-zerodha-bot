import sys; sys.path.insert(0,'.')
from app.kite_session import get_session_manager
kite = get_session_manager().get_kite()
nfo = kite.instruments('NFO')

names = set(r.get('name','') for r in nfo if 'NIFTY' in r.get('name','') and r['instrument_type']=='FUT')
print('NIFTY future names:', sorted(names)[:20])

nf = [r for r in nfo if r.get('name')=='NIFTY' and r['instrument_type'] in ('CE','PE')]
expiries = sorted({r['expiry'] for r in nf})
print(f'NIFTY option expiries ({len(expiries)}): {expiries[:20]}')

futs = sorted([r for r in nfo if r.get('name')=='NIFTY' and r['instrument_type']=='FUT'], key=lambda x: x['expiry'])
print(f'NIFTY futures: {[(f["tradingsymbol"], str(f["expiry"])) for f in futs]}')
print(f'Lot size: {futs[0]["lot_size"] if futs else "??"}')

# Weekly vs monthly distinction (monthly = last Thursday of month)
import datetime
weekly = [e for e in expiries if e.month == e.month]
print('\nAll expiries with weekday:')
for e in expiries[:20]:
    d = datetime.date(e.year, e.month, e.day)
    # Last Thursday of month?
    import calendar
    last_thu = max(w[3] for w in calendar.monthcalendar(e.year, e.month) if w[3])
    is_monthly = (e.day == last_thu)
    print(f'  {e}  {d.strftime("%a")}  {"MONTHLY" if is_monthly else "weekly"}')
