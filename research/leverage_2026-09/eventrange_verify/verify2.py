import sys; sys.argv = ['run.py', 'main']
import numpy as np, run
r, tr, st, _ = run.run_sleeve('grid', dict(g=0.02, N=5, centre='ema168', brk='stop'), 'DOGE', 5, 'OOS')
k = np.nonzero(r <= -0.999)[0]
print('liq day index', k, run.DAYS['OOS'][k[-1]].date(), 'post-revival compounded equity', np.prod(1 + r[k[-1] + 1:]))
