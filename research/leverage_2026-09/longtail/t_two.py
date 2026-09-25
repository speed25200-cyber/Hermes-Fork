import numpy as np, pandas as pd
from lt_core import *
D=Data()
R=24; rows=D.rows(R, IS0, IS1)
e=D.elig('TAIL', rows, 'r1d')
p=pct_rank(D.f['r1d'][rows], e)
Wt=build_weights(p, e, D.f['vol'][rows], D.f['beta'][rows])
fw=np.expm1(np.nan_to_num(D.f['fwd24'][rows]))
ideal=(Wt*fw).sum(1)
print('ideal per-day mean %.5f sd %.5f sharpe %.2f'%(ideal.mean(), ideal.std(), ideal.mean()/ideal.std()*np.sqrt(365)))
# equal-weight version
We=np.where(Wt>0, 0.5/np.maximum((Wt>0).sum(1,keepdims=True),1), np.where(Wt<0,-0.5/np.maximum((Wt<0).sum(1,keepdims=True),1),0))
ie=(We*fw).sum(1); print('EW ideal mean %.5f sharpe %.2f'%(ie.mean(), ie.mean()/ie.std()*np.sqrt(365)))
lg=(np.where(Wt>0,Wt,0)*fw).sum(1); sh=(np.where(Wt<0,Wt,0)*fw).sum(1)
print('long leg contrib %.5f short leg contrib %.5f'%(lg.mean(), sh.mean()))
res0=simulate(D, rows, Wt, IS0, IS1, cost_mult=0.0, use_lots=False); r=daily_returns(res0['eq']); print('sim gross daily mean %.5f sharpe %.2f n %d'%(r.mean(), sharpe(r), len(r)))
print(metrics(res0))
# median and trimmed
print('ideal pct days>0', (ideal>0).mean(), 'worst days', np.sort(ideal)[:5], 'best', np.sort(ideal)[-5:])
