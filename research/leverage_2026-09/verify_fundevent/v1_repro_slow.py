"""Verifier step 1: reproduce the reported slow-family (iii) numbers for the IS-selected config at every leverage,
with the researcher's own engine (copied unchanged into vslow.py; the verifier switches default to off)."""
import json, sys, time, os
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vslow as fs
HERE = os.path.dirname(os.path.abspath(__file__))
t = time.time(); fs.load(); print('loaded', round(time.time() - t), 's', flush=True)
cfg = (2.0, 0.0, 10, 'btc', 'both', 'all')
rows = []
for L in fs.LEVS:
    r = fs.run_one((cfg, L))
    rows.append(r)
    print(L, 'IS %.4f OOS %.4f ddOOS %.4f wdOOS %.4f shOOS %.3f liqOOS %d trIS %d trOOS %d  %s' % (
        r['cagr_is'], r['cagr_oos'], r['maxdd_oos'], r['worst_day_oos'], r['sharpe_oos'], r['liq_oos'], r['trades_is'], r['trades_oos'], r['years_oos']), flush=True)
pd.DataFrame(rows).to_csv(os.path.join(HERE, 'v1_repro_slow.csv'), index=False)
g = pd.read_parquet('/dev/shm/fundevent/grid_slow.parquet')
m = (g.thr_in == 2.0) & (g.thr_out_f == 0.0) & (g.K == 10) & (g.hedge == 'btc') & (g.side == 'both') & (g.universe == 'all')
print(g[m].sort_values('L')[['L', 'cagr_is', 'cagr_oos', 'maxdd_oos', 'liq_oos', 'trades_is', 'trades_oos']].to_string())
