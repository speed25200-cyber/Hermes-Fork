import pandas as pd, numpy as np, json, multiprocessing as mp
from load import load
import cb_v as cb
AS = load()
B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
IS = ('2022-01-01', '2024-12-31 23:55'); OOS = ('2025-01-01', '2026-08-31 23:55')
LEVS = [1, 10, 15, 20]
def one(arg):
    tag, p, L, per = arg
    a, b = per
    sims, m, ec = cb.run_assets([AS['BTC'], AS['ETH']], p, L, 'cross', a, b)
    return dict(tag=tag, L=L, start=a, cagr=round(m['cagr']*100, 2), dd=round(m['maxdd']*100, 1), sh=round(m['sharpe'], 2),
                wd=round(m['worst_day']*100, 1), liq=m['nliq'], n=m['nentry'])
if __name__ == '__main__':
    args = []
    variants = {'base': B, 'delay1h': dict(B, delay=12), 'delay30m': dict(B, delay=6), 'fill_worst_last_mark': dict(B, fill='worst'),
                'fill_mark': dict(B, fill='mark'), 'worst+delay1h': dict(B, fill='worst', delay=12)}
    for tag, p in variants.items():
        for L in LEVS:
            for per in (IS, OOS):
                args.append((tag, p, L, per))
    with mp.Pool(4) as pool:
        res = pool.map(one, args)
    df = pd.DataFrame(res); df['per'] = np.where(df.start == IS[0], 'IS', 'OOS')
    print(df.pivot_table(index=['tag', 'L'], columns='per', values=['cagr', 'dd', 'liq'], sort=False).to_string())
    df.to_csv('stress_exec.csv', index=False)
    # start-date sensitivity (OOS start shifted weekly over Q1 2025, parameters frozen, starting flat)
    starts = [str(d.date()) for d in pd.date_range('2025-01-01', '2025-06-30', freq='7D')]
    args = [('start', B, L, (s0, OOS[1])) for s0 in starts for L in [1, 10, 15, 20]]
    with mp.Pool(4) as pool:
        res = pool.map(one, args)
    ds = pd.DataFrame(res)
    print(ds.pivot_table(index='start', columns='L', values='cagr').to_string())
    print(ds.groupby('L').cagr.describe().round(2).to_string())
    ds.to_csv('stress_start.csv', index=False)
