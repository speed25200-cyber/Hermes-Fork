"""Capacity check: OKX quote volume (USDT) in the entry hours (t0+24, t0+72) for traded OKX-extra contracts still
listed (REST history-candles), vs traded Binance-set events 2025-26 on their OKX contract."""
import sys, json, pandas as pd, numpy as np
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist')
from common import okx_get
from concurrent.futures import ThreadPoolExecutor
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
live = {d['instId'] for d in okx_get('/api/v5/public/instruments?instType=SWAP')}
otr = pd.read_csv(SP + '/xlist/results/okx_trades.csv')
oev = pd.read_parquet(SP + '/xlist/data/sim/events.parquet')
jobs = []
for _, t in otr.iterrows():
    if t.sym in live:
        t0 = int(oev[oev.sym == t.sym].t0.iloc[0])
        jobs.append(('okx_extra', t.sym, t0, int(t.tranche), float(t.notional)))
btr = pd.read_csv(SP + '/xlist/verify/stat/s1_trades_port_union.csv') if False else None
bev = pd.read_parquet(SP + '/newlisting/data/events.parquet')
bt = pd.read_csv(SP + '/xlist/verify/premkt/bn_trades_base.csv')
print(bt.columns.tolist()[:20])
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.data.universe import base_asset
for _, t in bt.iterrows():
    r = bev[bev.sym == t.sym].iloc[0]
    if pd.to_datetime(r.t0, unit='ms') < pd.Timestamp('2025-01-01'):
        continue
    inst = base_asset(t.sym) + '-USDT-SWAP'
    if inst in live:
        jobs.append(('binance', inst, int(r.t0), int(t.tranche), float(t.notional)))
print('jobs', len(jobs))
cache = {}
def fetch(key):
    inst, t0 = key
    d = okx_get(f'/api/v5/market/history-candles?instId={inst}&bar=1H&after={t0 + 100 * 3600000}&limit=100')
    return key, d
keys = sorted({(j[1], j[2]) for j in jobs})
with ThreadPoolExecutor(6) as ex:
    for k, d in ex.map(fetch, keys):
        cache[k] = {int(x[0]): float(x[7]) for x in (d or [])}
rows = []
for src, inst, t0, k, notional in jobs:
    t0h = t0 // 3600000 * 3600000
    h = t0h + (24 if k == 0 else 72) * 3600000
    v = cache[(inst, t0)].get(h, np.nan)
    v3 = np.nansum([cache[(inst, t0)].get(h + j * 3600000, np.nan) for j in range(3)])
    rows.append(dict(src=src, inst=inst, tranche=k, notional_frac=notional, qv_entry_hour=v, qv_3h=v3))
df = pd.DataFrame(rows)
df.to_csv(SP + '/xlist/verify/critic/c4_liq.csv', index=False)
for s, g in df.groupby('src'):
    q = g.qv_entry_hour.dropna()
    print(s, 'n', len(g), 'with vol', len(q), 'entry-hour quote vol USDT: p10 %.0f p25 %.0f median %.0f' % tuple(np.percentile(q, [10, 25, 50])),
          '| min %.0f' % q.min())
    # tranche notional in USDT = frac * NAV ; participation of a NAV=10k/100k/1M sleeve in the entry hour
    for nav in (1e4, 1e5, 1e6):
        part = g.notional_frac * nav / g.qv_entry_hour
        print(f'   NAV {nav:>9,.0f}: tranche/entry-hour volume median {part.median():.4f} p90 {part.quantile(.9):.4f} max {part.max():.4f}; share >5%: {(part > .05).mean():.2f}')
