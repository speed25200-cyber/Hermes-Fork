import sys, os, json, time
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue')
import numpy as np, pandas as pd
from multiprocessing import Pool
import xvenue_bt as bt
IS = dict(start='2022-01-01', end='2025-01-01'); OOS = dict(start='2025-01-01', end='2026-09-01')
S = pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/results/summary.csv')
S = S[S.family == 'funding_diff_carry']
def cfg(row):
    c = json.loads(row['config']); c['L'] = float(row['L_per_venue']); return c
def _run(p):
    r = bt.run(p); return {k: v for k, v in r.items() if not k.startswith('_')}
if __name__ == '__main__':
    jobs, tags = [], []
    for _, row in S.iterrows():
        if row.L_per_venue > 20: continue
        for per, pr in [('IS', IS), ('OOS', OOS)]:
            jobs.append(dict(cfg(row), **pr)); tags.append((row.L_per_venue, per))
    t = time.time()
    bt.load_panel()
    with Pool(4) as pool: res = pool.map(_run, jobs, chunksize=1)
    rows = []
    for (L, per), r in zip(tags, res):
        d = dict(L=L, period=per, cagr=r['cagr'], maxdd=r['maxdd'], worst_day=r['worst_day'], sharpe=r['sharpe'], liq=r['liq'], cuts=r['cuts'], entries=r['entries'],
                 funding=r['funding_pct'], fees=r['fees_pct'], slip=r['slip_pct'])
        d.update({'y'+k: v for k, v in r['yearly'].items()}); rows.append(d)
    df = pd.DataFrame(rows); pd.set_option('display.width', 250)
    print(df.round(4).to_string()); print(round(time.time()-t), 's')
    df.to_csv('repro_base.csv', index=False)
