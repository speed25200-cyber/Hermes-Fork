"""Verifier step 8: slow-grid (researcher's grid_slow.parquet) selection diagnostics at each leverage:
Spearman(IS CAGR, OOS CAGR), OOS of the IS top-decile, one-step neighbours of the selected config."""
import os, numpy as np, pandas as pd
from scipy.stats import spearmanr
HERE = os.path.dirname(os.path.abspath(__file__))
g = pd.read_parquet('/dev/shm/fundevent/grid_slow.parquet')
rows = []
for L in [1, 3, 5, 10, 15, 20]:
    x = g[g.L == L]
    rho = spearmanr(x.cagr_is, x.cagr_oos).correlation
    top = x[x.cagr_is >= x.cagr_is.quantile(0.9)]
    xo = x[x.universe == 'okx']
    rows.append({'L': L, 'spearman_is_oos': rho, 'top10pct_IS_n': len(top), 'top10pct_IS_median_oos': top.cagr_oos.median(),
                 'top10pct_IS_oos_pos': int((top.cagr_oos > 0).sum()), 'okx_n': len(xo), 'okx_oos_pos': int((xo.cagr_oos > 0).sum()),
                 'okx_both_pos': int(((xo.cagr_is > 0) & (xo.cagr_oos > 0)).sum()), 'okx_median_oos': xo.cagr_oos.median()})
print(pd.DataFrame(rows).round(3).to_string(index=False))
base = dict(thr_in=2.0, thr_out_f=0.0, K=10, hedge='btc', side='both', universe='all')
alts = dict(thr_in=[0.5, 1.0, 4.0], thr_out_f=[0.5], K=[1, 3], hedge=['none'], side=['pos', 'neg'], universe=['okx'])
x1 = g[g.L == 1]
out = []
for k, vals in alts.items():
    for v in vals:
        c = dict(base); c[k] = v
        m = np.ones(len(x1), bool)
        for kk, vv in c.items():
            m &= (x1[kk] == vv).values
        r = x1[m].iloc[0]
        out.append({'change': f'{k}={v}', 'cagr_is': r.cagr_is, 'cagr_oos': r.cagr_oos, 'maxdd_oos': r.maxdd_oos, 'trades_oos': r.trades_oos, 'liq_oos': r.liq_oos})
nb = pd.DataFrame(out)
print(nb.round(3).to_string(index=False))
print('neighbours OOS>0:', int((nb.cagr_oos > 0).sum()), 'of', len(nb), '; median neighbour OOS CAGR', round(nb.cagr_oos.median(), 3))
pd.DataFrame(rows).to_csv(os.path.join(HERE, 'v8_grid_stats.csv'), index=False)
nb.to_csv(os.path.join(HERE, 'v8_neighbours_1x.csv'), index=False)
