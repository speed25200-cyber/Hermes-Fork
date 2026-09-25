import json, numpy as np, pandas as pd
from lt_core import *
D=Data(); signs=json.load(open(f"{W}/out/signs_is.json"))
R=8; univ='TAIL'; s='fund'
rows_all=D.rows(R, IS0, OOS1); e=D.elig(univ, rows_all, s); p=pct_rank(D.f[s][rows_all], e)
Wt=build_weights((p-0.5)*signs[f"{univ}|{R}|{s}"], e, D.f['vol'][rows_all], D.f['beta'][rows_all])
msk=D.dec_time[rows_all]>=OOS0
res=simulate_maker(D, rows_all[msk], Wt[msk], OOS0, OOS1, off=15, fallback=0, univ=univ)
eq=res['eq']; print('liq', res['liq']); r=eq.pct_change()
print(r.sort_values().head(8))
pc=pd.Series(res['pnl_coin'], index=D.syms).sort_values(); print(pc.head(8)); print(pc.tail(5))
h=np.searchsorted(D.hrs, (pd.Timestamp('2026-06-05 09:00', tz='UTC')-pd.Timestamp('1970-01-01',tz='UTC'))//pd.Timedelta(hours=1))
print('bar', pd.to_datetime(D.hrs[h]*3600, unit='s'))
mv = D.H[h]/D.Cf[h-1]-1; lo = D.Lo[h]/D.Cf[h-1]-1
o=np.argsort(-mv)[:5]; print([(D.syms[j], round(mv[j],3), round(D.Cf[h-1,j],6), round(D.H[h,j],6), round(D.Cf[h,j],6)) for j in o])
o=np.argsort(lo)[:5]; print([(D.syms[j], round(lo[j],3)) for j in o])
