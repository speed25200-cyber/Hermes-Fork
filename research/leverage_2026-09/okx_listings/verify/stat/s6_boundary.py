"""Sensitivity of the period narrative to the split date (slices of the full 2022-01..2026-08 daily returns, s1).
-> s6_boundary.json"""
import json, numpy as np, pandas as pd
from statlib import sharpe, OUT
d = pd.read_parquet(f'{OUT}/s1_daily.parquet')
res = {}
for v in ('union', 'okx_half', 'okx_nobn'):
    u = d[f'{v} | 2022-2026'].dropna(); b = d['binance_only | 2022-2026'].dropna(); o = d[f'{v}|alone | 2022-2026'].dropna()
    for split in ('2024-11-01', '2025-01-01', '2025-02-01', '2025-04-01'):
        pre, post = u.index < split, u.index >= split
        rec = dict(pre=dict(okx_alone=round(sharpe(o[pre]), 3), union=round(sharpe(u[pre]), 3), bn=round(sharpe(b[pre]), 3),
                            d=round(sharpe(u[pre]) - sharpe(b[pre]), 3)),
                   post=dict(okx_alone=round(sharpe(o[post]), 3), union=round(sharpe(u[post]), 3), bn=round(sharpe(b[post]), 3),
                             d=round(sharpe(u[post]) - sharpe(b[post]), 3)))
        res[f'{v} split {split}'] = rec
        print(v, split, rec)
    ep = (u.index >= '2024-11-01') & (u.index < '2025-02-01')
    res[f'{v} Nov24-Jan25 episode'] = dict(okx_alone_ret=round(float(np.prod(1 + o[ep]) - 1), 4), union_ret=round(float(np.prod(1 + u[ep]) - 1), 4),
                                           bn_ret=round(float(np.prod(1 + b[ep]) - 1), 4),
                                           ex_episode=dict(okx_alone=round(sharpe(o[~ep]), 3), union=round(sharpe(u[~ep]), 3), bn=round(sharpe(b[~ep]), 3)))
    print(v, 'episode', res[f'{v} Nov24-Jan25 episode'])
json.dump(res, open(f'{OUT}/s6_boundary.json', 'w'), indent=1)
