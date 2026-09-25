import time, numpy as np, pandas as pd
from lt_core import *
t=time.time(); D=Data(); print('load', round(time.time()-t,1)); print('unknown-OKX coins', int((D.lot_q==0).sum()), 'of', D.N)
R=24; rows=D.rows(R, IS0, IS1)
e=D.elig('TAIL', rows, 'r1d')
print('rows', len(rows), 'elig per row median', np.median(e.sum(1)))
t=time.time(); p=pct_rank(D.f['r1d'][rows], e); print('pct', round(time.time()-t,1))
ic=ic_series(p, D.f['fwd24'][rows], e); print('IC r1d IS mean', np.nanmean(ic), 't', np.nanmean(ic)/np.nanstd(ic)*np.sqrt(np.isfinite(ic).sum()))
Wt=build_weights(p, e, D.f['vol'][rows], D.f['beta'][rows])
print('gross', np.abs(Wt).sum(1).mean(), 'net', Wt.sum(1).mean())
t=time.time(); res=simulate(D, rows, Wt, IS0, IS1); print('sim', round(time.time()-t,1))
t=time.time(); res=simulate(D, rows, Wt, IS0, IS1); print('sim2', round(time.time()-t,1))
print(metrics(res))
res0=simulate(D, rows, Wt, IS0, IS1, cost_mult=0.0); print('gross', metrics(res0))
