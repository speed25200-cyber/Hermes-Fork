"""Build the daily panel for the diversified TSMOM study (read-only reuse of earlier-round caches).

Sources (all real data, earlier rounds):
  newlisting/data/daily_klines.parquet : Binance USD-M daily OHLCV, all 864 USDT perps incl. delisted (2020-01..2026-08)
  newlisting/data/funding.parquet      : Binance USD-M funding events (739 symbols)
  newlisting/data/okx_calendar.parquet : day x symbol, True iff the OKX USDT swap traded that day (OKX trade archives)
  longtail/venue/okx_instruments_now.json, okx_tiers_now.json : OKX contract specs and tier-1 MMR (2026-09-25)
-> panel.npz (dates x symbols arrays), meta.json
"""
import json, os
import numpy as np, pandas as pd

SP = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad'
OUT = f'{SP}/tsmom_div'

dk = pd.read_parquet(f'{SP}/newlisting/data/daily_klines.parquet')
dk['d'] = pd.to_datetime(dk.t, unit='ms', utc=True).dt.normalize()
dk = dk.drop_duplicates(['sym', 'd'], keep='last')
cal = pd.read_parquet(f'{SP}/newlisting/data/okx_calendar.parquet')
cal.index = pd.to_datetime(cal.index, utc=True)

EXCL = {'USDCUSDT', 'BUSDUSDT', 'TUSDUSDT', 'FDUSDUSDT', 'USDPUSDT', 'DAIUSDT', 'USDEUSDT', 'BTCDOMUSDT',
        'DEFIUSDT', 'BLUEBIRDUSDT', 'FOOTBALLUSDT', 'USD1USDT', 'EURUSDT', 'GBPUSDT', 'AEURUSDT', 'RLUSDUSDT',
        'XUSDUSDT', 'BFUSDUSDT', 'USDTUSDT'}
syms = sorted(s for s in dk.sym.unique() if s.endswith('USDT') and s not in EXCL and s in cal.columns
              and cal[s].any())
dates = pd.date_range('2020-01-01', '2026-08-31', freq='D', tz='UTC')
print('symbols ever on OKX and Binance:', len(syms), 'days', len(dates))


def piv(col):
    p = dk[dk.sym.isin(syms)].pivot(index='d', columns='sym', values=col)
    return p.reindex(index=dates, columns=syms)


O, H, Lw, C, QV = (piv(c) for c in ['o', 'h', 'l', 'c', 'qv'])
# a bar is "live" if it exists and has non-zero volume
live = C.notna() & (QV > 0)

# funding: event at time T belongs to the holding day that ends at T (subtract 5 min, floor to day)
fu = pd.read_parquet(f'{SP}/newlisting/data/funding.parquet')
fu = fu[fu.sym.isin(syms)]
fu['d'] = pd.to_datetime(fu.t - 5 * 60 * 1000, unit='ms', utc=True).dt.normalize()
F = fu.groupby(['d', 'sym']).rate.sum().unstack().reindex(index=dates, columns=syms).fillna(0.0)

okx = cal.reindex(index=dates, columns=syms).fillna(False).astype(bool)

# OKX specs today (for lot sizes / MMR / max leverage)
inst = json.load(open(f'{SP}/longtail/venue/okx_instruments_now.json'))
tiers = json.load(open(f'{SP}/longtail/venue/okx_tiers_now.json'))
spec = {}
by_id = {i['instId']: i for i in inst}
for s in syms:
    base = s[:-4]
    cands = [base]
    for pre in ('1000000', '1000', '1M'):
        if base.startswith(pre):
            cands.append(base[len(pre):])
    for b in cands:
        iid = f'{b}-USDT-SWAP'
        if iid in by_id:
            i = by_id[iid]
            t1 = tiers.get(f'{b}-USDT', [{}])[0]
            spec[s] = dict(instId=iid, ctVal=float(i['ctVal']), lotSz=float(i['lotSz']), minSz=float(i['minSz']),
                           lever=float(i['lever']), state=i['state'], mmr1=t1.get('mmr'), imr1=t1.get('imr'),
                           maxLever1=t1.get('maxLever'), tier1_max=t1.get('maxSz'))
            break
print('symbols with current OKX spec:', len(spec))
np.savez_compressed(f'{OUT}/panel.npz', O=O.values, H=H.values, L=Lw.values, C=C.values, QV=QV.values,
                    live=live.values, F=F.values, okx=okx.values,
                    dates=np.array([str(d.date()) for d in dates]), syms=np.array(syms))
json.dump(spec, open(f'{OUT}/okx_spec_now.json', 'w'), indent=0)
print('done')
