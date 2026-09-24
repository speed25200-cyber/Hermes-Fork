"""Verifier step 5: where exactly does the price move at an extreme-funding settlement happen?
Uses the researcher's 1m windows (bar 61 = bar opening at t). Side = the side that RECEIVES the realised funding at t
(oracle side, to measure the mechanism).  Mean side-signed log returns in bp, raw and BTC-beta hedged, for
consecutive 1-minute steps around t, clustered t-stat by settlement time."""
import os, numpy as np, pandas as pd
D = '/dev/shm/fundevent'
PRE = 61
ev = pd.read_parquet(os.path.join(D, 'ev.parquet'), columns=['sym', 't', 'rate', 'ann', 'ann_prev', 'beta', 'row'])
W = np.load(os.path.join(D, 'win_perp.npy'), mmap_mode='r')
WB = np.load(os.path.join(D, 'win_btc.npy'), mmap_mode='r')
assert (ev.row.values == np.arange(len(ev))).all()
O = np.asarray(W[:, 55:70, 0], dtype=np.float64)      # bars t-6 .. t+8
BO = np.asarray(WB[:, 55:70, 0], dtype=np.float64)
def col(k): return k + PRE - 55
per = np.where(ev.t.values < pd.Timestamp('2025-01-01').value // 10 ** 6, 'IS', 'OOS')
rows = []
for thr in [2.0, 4.0]:
    for sgn in ['neg', 'pos']:
        for p in ['IS', 'OOS']:
            m = (per == p) & (np.abs(ev.ann.values) >= thr) & ((ev.ann.values < 0) if sgn == 'neg' else (ev.ann.values > 0))
            s = -np.sign(ev.ann.values[m])
            rec = {'thr': thr, 'funding_sign': sgn, 'period': p, 'n': int(m.sum()), 'fund_recv_bp': float(np.mean(np.abs(ev.rate.values[m])) * 1e4)}
            for a, b in [(-5, -1), (-2, -1), (-1, 0), (0, 1), (1, 2), (2, 5), (5, 8)]:
                rc = np.log(O[m, col(b)] / O[m, col(a)]); rb = np.log(BO[m, col(b)] / BO[m, col(a)])
                x = s * (rc - ev.beta.values[m] * rb) * 1e4
                ok = np.isfinite(x)
                g = pd.Series(x[ok]).groupby(ev.t.values[m][ok]).mean()
                rec[f'h{a}_{b}'] = round(float(x[ok].mean()), 1)
                rec[f't{a}_{b}'] = round(float(g.mean() / (g.std() / np.sqrt(len(g)))), 1)
            rows.append(rec)
df = pd.DataFrame(rows)
df.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'v5_settlement_move.csv'), index=False)
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40)
print(df.to_string(index=False))
