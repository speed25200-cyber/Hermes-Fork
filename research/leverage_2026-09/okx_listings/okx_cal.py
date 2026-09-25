"""OKX USDT-swap listing calendar, delisted included.
- 2022-01-01 .. 2025-09-07: OKX all-swaps daily funding files (xvenue cache) -> first/last funding per instId.
- current instruments (SWAP + SPOT) from the public REST API: listTime, category, state.
- 2025-09-08 .. now, delisted since: candidates = Bybit perps (public archive, delisted included) and Binance UM
  perps first seen since 2025-08-15, mapped to OKX ids, not live now -> dense probes of the OKX daily trade archive.
-> data/okx_swaps.csv (instId, first_day8, last_day8, src, category, live, listTime)"""
import sys, re
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import *
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.data.universe import base_asset

a = pd.read_parquet(os.path.join(os.path.dirname(BASE), 'xvenue', 'data', 'okx_funding_all.parquet'))
g = a.groupby('instId').funding_time.agg(['min', 'max'])
rows = {i: dict(instId=i, first=int(r['min']), last=int(r['max']), src='allswaprate') for i, r in g.iterrows()}
sw = okx_get('/api/v5/public/instruments?instType=SWAP')
sp = okx_get('/api/v5/public/instruments?instType=SPOT')
json.dump(sw, open(os.path.join(D, 'okx_swap_instruments.json'), 'w'))
json.dump(sp, open(os.path.join(D, 'okx_spot_instruments.json'), 'w'))
live = {x['instId']: x for x in sw if x['instId'].endswith('-USDT-SWAP')}
for i, x in live.items():
    r = rows.setdefault(i, dict(instId=i, first=int(x['listTime']), last=None, src='live'))
    r.update(category=x.get('instCategory'), live=True, listTime=int(x['listTime']), state=x['state'])
# gap-period candidates
by = json.load(open(os.path.join(BASE, 'bybit_calendar.json')))['trading']
cands = {}
for s, v in by.items():
    if v and s.endswith('USDT') and v[0] >= '2025-08-15':
        cands[f'{base_asset(s)}-USDT-SWAP'] = pd.Timestamp(v[0])
ev = pd.read_parquet(os.path.join(NL, 'events.parquet'))
for s, t0 in zip(ev.sym, ev.t0):
    if t0 >= pd.Timestamp('2025-08-15').value // 10 ** 6:
        cands.setdefault(f'{base_asset(s)}-USDT-SWAP', pd.Timestamp(t0, unit='ms').normalize())
cands = {i: d for i, d in cands.items() if i.isascii() and i not in live and not (i in rows and rows[i]['last'] and rows[i]['last'] >= pd.Timestamp('2025-09-01').value // 10 ** 6)}
print('gap candidates', len(cands), flush=True)
jobs = [(i, d + pd.Timedelta(days=k)) for i, d in cands.items() for k in range(-5, 36) if d + pd.Timedelta(days=k) <= pd.Timestamp('2026-09-24')]


def probe(j):
    i, d = j
    try:
        return j, get(trades_url(i, d), head=True) is not None
    except RuntimeError:
        return j, None


with ThreadPoolExecutor(48) as ex:
    res = list(ex.map(probe, jobs))
hit = {}
print('probe failures', sum(ok is None for _, ok in res), flush=True)
for (i, d), ok in res:
    if ok:
        hit.setdefault(i, []).append(d)
for i, ds in hit.items():
    first = min(ds) - pd.Timedelta(hours=8)          # archive day (UTC+8) -> UTC start
    rows.setdefault(i, dict(instId=i, src='probe'))
    rows[i].update(first=int(first.value // 10 ** 6), last=int((max(ds) + pd.Timedelta(hours=16)).value // 10 ** 6),
                   src=rows[i].get('src', 'probe') + '+probe', live=False)
print('gap delisted found on OKX', len(hit), sorted(hit)[:40], flush=True)
out = pd.DataFrame(rows.values())
out['live'] = out['live'].fillna(False)
out.to_csv(os.path.join(D, 'okx_swaps.csv'), index=False)
print(out.shape, out.src.value_counts().to_dict())
