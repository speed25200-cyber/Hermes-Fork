"""Family (ii) descriptive: price drift of extreme-funding perps around settlement.
Returns in the direction of the side that RECEIVES the funding (side = -sign(ann_prev), known before t), in bp,
raw coin and BTC-beta-hedged, per window; mean and t-stat clustered by settlement time (mean per t, then
t-stat across settlement times). IS 2022-2024 vs OOS 2025-01..2026-08. Also the realised funding at t (bp) for
comparison. -> drift_stats.csv
"""
import os
import numpy as np, pandas as pd
from common import D

PRE = 61
WINS = [(-60, -30), (-30, -15), (-15, -5), (-5, -1), (-1, 1), (1, 5), (5, 15), (15, 30), (30, 60), (-30, 30), (-5, 5)]
ev = pd.read_parquet(os.path.join(D, 'ev.parquet'))
W = np.load(os.path.join(D, 'win_perp.npy'), mmap_mode='r')
WB = np.load(os.path.join(D, 'win_btc.npy'), mmap_mode='r')
O = np.asarray(W[:, :, 0], dtype=np.float64)
BO = np.asarray(WB[:, :, 0], dtype=np.float64)
s = -np.sign(ev.ann_prev.values)
beta = ev.beta.values
per = np.where(ev.t.values < pd.Timestamp('2025-01-01').value // 10 ** 6, 'IS', 'OOS')
rows = []
for thr in [0.5, 1.0, 2.0, 4.0]:
    for sgn in ['pos', 'neg', 'all']:
        m0 = np.abs(ev.ann_prev.values) >= thr
        if sgn == 'pos':
            m0 &= ev.ann_prev.values > 0
        elif sgn == 'neg':
            m0 &= ev.ann_prev.values < 0
        for p in ['IS', 'OOS']:
            m = m0 & (per == p)
            fund = (-s * ev.rate.values)[m] * 1e4
            rec = {'thr': thr, 'side_of_funding': sgn, 'period': p, 'n': int(m.sum()), 'n_times': int(ev.t[m].nunique()),
                   'funding_recv_bp': float(np.nanmean(fund))}
            for a, b in WINS:
                rc = np.log(O[m, b + PRE] / O[m, a + PRE])
                rbt = np.log(BO[m, b + PRE] / BO[m, a + PRE])
                raw = s[m] * rc * 1e4
                hed = s[m] * (rc - beta[m] * rbt) * 1e4
                for nm, x in (('raw', raw), ('hedged', hed)):
                    ok = np.isfinite(x)
                    g = pd.Series(x[ok]).groupby(ev.t.values[m][ok]).mean()
                    rec[f'{nm}_{a}_{b}'] = float(np.mean(x[ok]))
                    rec[f't_{nm}_{a}_{b}'] = float(g.mean() / (g.std() / np.sqrt(len(g)))) if len(g) > 2 else np.nan
            rows.append(rec)
df = pd.DataFrame(rows)
df.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'drift_stats.csv'), index=False)
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40)
cols = ['thr', 'side_of_funding', 'period', 'n', 'funding_recv_bp'] + [f'hedged_{a}_{b}' for a, b in WINS]
print(df[cols].round(1).to_string())
cols = ['thr', 'side_of_funding', 'period'] + [f't_hedged_{a}_{b}' for a, b in WINS]
print(df[cols].round(1).to_string())
