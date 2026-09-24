import pandas as pd, numpy as np
from load import load, cb
AS = load()
p = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
PER = {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}
rows = []
for L in [1, 3, 5, 10, 15, 20]:
    for per, (a, b) in PER.items():
        sims, m, ec = cb.run_assets([AS['BTC'], AS['ETH']], p, L, 'cross', a, b)
        rows.append(dict(L=L, per=per, cagr=round(m['cagr']*100, 2), dd=round(m['maxdd']*100, 1), sh=round(m['sharpe'], 2),
                         wd=round(m['worst_day']*100, 1), liq=m['nliq'], n=m['nentry'], roll=m['nroll'],
                         **{y: round(v*100, 1) for y, v in m['years'].items()}))
        print(rows[-1], flush=True)
pd.DataFrame(rows).to_csv('repro_shortB.csv', index=False)
