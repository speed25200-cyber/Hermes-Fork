"""OKX tick size (current tickSz) in bp of the median OOS price, for every coin in the OOS selections of the two
schemes holding the IS picks. Checks the 1-bp trade-through proxy against 'at least one tick'."""
import json, numpy as np, pandas as pd, sys
sys.path.insert(0, '.')
from pairs_bt import *
inst = json.load(open('out/okx_inst.json'))['data']
tick = {d['instFamily'].replace('-USDT', ''): float(d['tickSz']) for d in inst if d['settleCcy'] == 'USDT'}
ctv = {d['instFamily'].replace('-USDT', ''): float(d['ctVal']) for d in inst if d['settleCcy'] == 'USDT'}
t = pd.read_csv('data/okx_tiers.csv').set_index('sym')
sel = json.load(open('out/selections.json'))
names = sorted(f[:-8] for f in os.listdir('data/h'))
rows = []
for key in ['coint|lvl|60', 'coint|ret|60']:
    for tt, v in sel[key].items():
        if pd.Timestamp(tt) < OOS0: continue
        for a, b, be in v:
            for s in (a, b):
                rows.append(names[s])
coins = sorted(set(rows))
out = []
for c in coins:
    d = pd.read_parquet(f'data/h/{c}.parquet', columns=['t', 'c'])
    d = d[d.t >= OOS0.value // 10**6]
    px = float(d.c.median())
    fam = t.okx.get(c)
    tk = tick.get(fam, np.nan)
    mult = 1000.0 if c.startswith('1000') and not fam.startswith('1000') else 1.0     # Binance 1000X vs OKX X
    out.append(dict(sym=c, okx=fam, tickSz=tk, px_binance=px, tick_bp=tk * mult / px * 1e4 if tk == tk else np.nan))
o = pd.DataFrame(out).sort_values('tick_bp', ascending=False)
pd.set_option('display.width', 200)
print(o.round(3).to_string(index=False))
print('coins', len(o), 'tick > 1bp:', int((o.tick_bp > 1).sum()), 'tick > 2bp:', int((o.tick_bp > 2).sum()), 'tick > 5bp:', int((o.tick_bp > 5).sum()))
o.to_csv('out/v7_ticks.csv', index=False)
