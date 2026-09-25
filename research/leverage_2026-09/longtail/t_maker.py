import numpy as np, pandas as pd
from lt_core import *
D=Data()
R=8; rows=D.rows(R, IS0, IS1)
e=D.elig('TAIL', rows, 'r4h'); p=pct_rank(D.f['r4h'][rows], e)
Wt=build_weights(p-0.5, e, D.f['vol'][rows], D.f['beta'][rows], hyst=True)
a=simulate(D, rows, Wt, IS0, IS1, lat=1)
b=simulate_maker(D, rows, Wt, IS0, IS1, off=1e6, fallback=1)
print('taker lat1 vs maker-never-fill+fallback: final eq', a['eq'].iloc[-1], b['eq'].iloc[-1], 'fill', b['fill_rate'])
for off in [0, 5, 15, 30]:
    for fb in [0,1]:
        m=simulate_maker(D, rows, Wt, IS0, IS1, off=off, fallback=fb)
        mt=metrics(m); print(off, fb, 'fill %.2f'%m['fill_rate'], 'IS sharpe %.2f cagr %.3f cost %.3f turn %.0f'%(mt['sharpe'], mt['cagr'], mt['cost_frac'], mt['turn_x']))
print('taker', metrics(simulate(D, rows, Wt, IS0, IS1))['sharpe'])
