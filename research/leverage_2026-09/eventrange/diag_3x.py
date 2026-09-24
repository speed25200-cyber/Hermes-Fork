import sys, json; sys.argv = ['run.py', 'main']
import numpy as np, pandas as pd, run
m = pd.read_csv('out/grid_wick_grid_orb.csv')
q = m[(m.family == 'wick') & (m.L == 3) & (m.period == 'OOS') & (m.liq > 0)]
print('wick 3x OOS configs with liquidations:', len(q))
cfg = json.loads(q.iloc[0].cfg); print(cfg)
for coin in run.COINS:
    r, tr, st, _ = run.run_sleeve('wick', cfg, coin, 3, 'OOS')
    for dd in np.nonzero(r <= -0.999)[0]:
        day = run.DAYS['OOS'][dd]
        d = run.D[coin]; a = np.searchsorted(d['t'], day.value // 10**6); b = a + 1440
        j = a + np.argmin(d['l'][a:b])
        print(coin, day.date(), 'worst 1m bar', pd.to_datetime(d['t'][j], unit='ms'), 'open', d['o'][j], 'low', d['l'][j], 'mark low', d['ml'][j], f"drop in bar {d['l'][j]/d['o'][j]-1:.1%}", f"mark drop {d['ml'][j]/d['o'][j]-1:.1%}")
