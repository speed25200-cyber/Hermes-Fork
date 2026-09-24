import time, sys
sys.path.insert(0, '.')
from pairs_bt import *
t=time.time()
P, syms, first_bar, U, mmr, imr = prepare()
print('load', time.time()-t, len(syms)); t=time.time()
sel = build_selections(P, U, first_bar)
print('sel', time.time()-t)
for key in [('coint','lvl',60),('coint','ret',120),('corr','ret',120),('fixed','lvl',120)]:
    for tt in [pd.Timestamp('2022-01-01'), pd.Timestamp('2022-06-01'), pd.Timestamp('2023-06-01'), pd.Timestamp('2025-03-01'), pd.Timestamp('2026-08-01')]:
        print(key, tt.date(), [(syms[a], syms[b], round(be,2)) for a,b,be in sel[key][tt][:5]], len(sel[key][tt]))
import collections
for key in sel:
    print(key, 'avg #pairs', np.mean([len(v) for v in sel[key].values()]))
