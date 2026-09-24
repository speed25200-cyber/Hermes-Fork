"""Verifier: single-episode dependence of the one positive headline (grid S1 at 1x, cfg 1: g=0.5% N=5 fixed centre, hold):
per-coin CAGR, leave-one-coin-out, leave-one-month-out, best month share, vs equal-weight buy-and-hold of the 7 coins."""
import sys, json
sys.argv = ['run.py', 'main']
import numpy as np, pandas as pd
import run
from common import COINS, combine, metrics
cfg = run.GRID_GRID[1]
for per in ('IS', 'OOS'):
    sl = {c: run.run_sleeve('grid', cfg, c, 1, per)[:2] for c in COINS}
    V, TR = combine(list(sl.values()), None, run.MONTHS[per]); m = metrics(V, TR, run.DAYS[per])
    loco = {c: round(metrics(*combine([sl[x] for x in COINS if x != c], None, run.MONTHS[per]), run.DAYS[per])['cagr'], 3) for c in COINS}
    percoin = {c: round(float(np.prod(1 + sl[c][0]) ** (365.25 / len(sl[c][0])) - 1), 3) for c in COINS}
    prevV = np.concatenate([[1.0], V[:-1]]); dr = V / prevV - 1
    mo = run.MONTHS[per]; mret = pd.Series(dr).groupby(mo).apply(lambda x: np.prod(1 + x) - 1)
    lomo = {}
    for mm in np.unique(mo):
        x = dr.copy(); x[mo == mm] = 0; lomo[int(mm)] = np.prod(1 + x) ** (365.25 / len(x)) - 1
    a, b = run.WIN[per]
    bh = np.mean([run.D[c]['c'][b - 1] / run.D[c]['o'][a] for c in COINS]) ** (365.25 / len(V)) - 1
    print(per, 'CAGR', round(m['cagr'], 4), 'maxdd', round(m['maxdd'], 3), '| per-coin', percoin, '| LOCO', loco,
          '| LOMO min', round(min(lomo.values()), 3), '| best month', round(mret.max(), 3), '| EW buy&hold CAGR (no rebal)', round(bh, 3))
