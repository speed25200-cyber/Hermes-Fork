import sys, numpy as np, pandas as pd
import run_grid as R, engine as E
from common import T0
cfg = tuple(eval(sys.argv[1])); mode = int(sys.argv[2]); lat = int(sys.argv[3]); li = int(sys.argv[4]); marg = sys.argv[5]
coins = sys.argv[6].split(',') if len(sys.argv) > 6 else ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT']
pd.set_option('display.width', 250)
for c in coins:
    P = R.prep(c)
    tr = R.trades_for(P, c, cfg, mode, lat)
    m = tr[:, E.col(marg, 'liq') + li] > 0
    for r in tr[m]:
        s, e, x = int(r[0]), int(r[1]), int(r[2])
        print(c, T0 + pd.Timedelta(minutes=s), 'side', r[3], 'b0 %.1f' % (r[11]*1e4), 'net %.1f bp' % ((r[4]-r[5]-r[6]+r[7])*1e4),
              'worst L %.3f' % r[E.col(marg,'worst')+li], 'ret %.3f' % r[E.col(marg,'ret')+li])
        seg = slice(e, x+1)
        mp = P['mpc'][seg]*1e4; bc = P['bc'][seg]*1e4; ic = P['ic'][seg]*1e4
        j = np.argmax(np.abs(mp))
        print('   max |mark prem| %.1f bp at +%d min; basis there %.1f; index-spot %.1f; spot move from entry %.2f%%' % (mp[j], j, bc[j], ic[j], (P['Sc'][e+j]/P['Sc'][e]-1)*100))
