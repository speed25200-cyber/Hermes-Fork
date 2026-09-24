"""Diagnostics: (1) liquidation dates per coin for representative configs at 10x/20x (what breaks them);
(2) grid 1x results vs directional exposure (beta of daily grid returns on the equal-weight coin return);
(3) ORB net vs gross for the US-open 60-min breakout configs."""
import sys, json
sys.argv = ['run.py', 'main']
import numpy as np, pandas as pd
import run
from common import COINS
per_days = run.DAYS
REPS = [('wick', 253), ('wick', 227), ('grid', 33), ('grid', 21), ('grid', 1), ('orb', 97),
        ('wick_s2', dict(w=5, z=10, V=10, entry='mkt', tp=1e6, stop=1e6, hold=60, side='long'))]
for fam, ci in REPS:
    cfg = ci if isinstance(ci, dict) else {'wick': run.WICK_GRID, 'grid': run.GRID_GRID, 'orb': run.ORB_GRID}[fam][ci]
    f = 'wick' if fam.startswith('wick') else fam
    for L in (10, 20):
        out = []
        for per in ('IS', 'OOS'):
            for coin in COINS:
                r, tr, st, trades = run.run_sleeve(f, cfg, coin, L, per)
                for dd in np.nonzero(r <= -0.999)[0]:
                    out.append(f'{coin}:{per_days[per][dd].date()}')
        print(fam, json.dumps(cfg), f'L={L}', 'liquidation/ruin days:', len(out), ' '.join(sorted(out, key=lambda s: s.split(':')[1])[:40]))
# (2) grid beta at 1x
cl = {}
for per in ('IS', 'OOS'):
    a, b = run.WIN[per]
    m = []
    for coin in COINS:
        d = run.D[coin]
        dayc = pd.Series(d['c'][a:b], index=d['day'][a:b]).groupby(level=0).last()
        m.append(dayc.pct_change().fillna(0).values)
    cl[per] = np.mean(m, axis=0)
for ci, cfg in enumerate(run.GRID_GRID):
    rows = []
    for per in ('IS', 'OOS'):
        rs = []
        for coin in COINS:
            r, tr, st, _ = run.run_sleeve('grid', cfg, coin, 1, per)
            rs.append(r)
        pr = np.mean(rs, axis=0)
        x = cl[per]
        beta = np.cov(pr, x)[0, 1] / x.var()
        corr = np.corrcoef(pr, x)[0, 1]
        rows.append(f'{per}: beta {beta:+.2f} corr {corr:+.2f}')
    print('grid 1x', json.dumps(cfg), ' | '.join(rows))
