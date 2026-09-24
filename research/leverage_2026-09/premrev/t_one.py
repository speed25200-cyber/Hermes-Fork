import sys, time, numpy as np, pandas as pd
import run_grid as R, engine as E
from common import T0
sym = sys.argv[1]; cfg = tuple(eval(sys.argv[2])); mode = int(sys.argv[3]); lat = int(sys.argv[4])
t = time.time(); P = R.prep(sym); print('prep', round(time.time()-t,1))
t = time.time(); tr = R.trades_for(P, sym, cfg, mode, lat); print('gen', round(time.time()-t,2), 'n', len(tr))
t = time.time(); tr = R.trades_for(P, sym, cfg, mode, lat); print('gen2', round(time.time()-t,3))
df = pd.DataFrame(tr[:, :13], columns=['sig','e','x','side','gross','fees','slip','fund','hours','mkin','mkout','b0','dev'])
df['t'] = T0 + pd.to_timedelta(df.sig, unit='min')
df['hold'] = df.x - df.e
df['net'] = df.gross - df.fees - df.slip + df.fund
for c in ['gross','fees','slip','fund','net','b0','dev']: df[c] = (df[c]*1e4).round(1)
for m in ['mc','pm','sep']:
    for f in ['ret','worst','liq','rej']:
        for li, L in enumerate(E.LEVS):
            df[f'{m}_{f}{int(L)}'] = tr[:, E.col(m, f) + li]
show = ['t','side','dev','b0','hold','gross','fees','slip','fund','net','mkin','mkout','mc_rej5','mc_rej10','mc_worst5','mc_liq5','pm_worst20','pm_liq10','pm_liq20','sep_rej5','sep_liq5']
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 400)
print(df[show].round(4).tail(int(sys.argv[5]) if len(sys.argv) > 5 else 40).to_string())
isx = df.sig < R.IS_B
for lab, m in [('IS', isx), ('OOS', ~isx)]:
    print(lab, 'n', m.sum(), 'mean net bp', df.net[m].mean().round(2), 'gross', df.gross[m].mean().round(2), 'median net', df.net[m].median(),
          'liq pm20', df.pm_liq20[m].sum(), 'liq pm10', df.pm_liq10[m].sum(), 'liq sep5', df.sep_liq5[m].sum(), 'mc rej5', df.mc_rej5[m].sum(), 'mc_rej10', df.mc_rej10[m].sum())
