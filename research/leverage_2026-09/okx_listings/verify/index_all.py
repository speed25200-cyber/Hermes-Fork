import sys; sys.path.insert(0,'.')
from restlib import candles
import pandas as pd, numpy as np
H=3600000; G0=pd.Timestamp('2021-12-01').value//10**6
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
tr=pd.read_csv(f'{SP}/results/okx_trades.csv'); ev=pd.read_parquet(f'{SP}/data/sim/events.parquet')
P=np.load(f'{SP}/data/sim/panel2.npz')
out={}
for sym in tr.sym.unique():
    i=int(ev.index[ev.sym==sym][0]); t0=int(ev.t0[i])
    ix=candles('history-index-candles',sym.replace('-SWAP',''),t0,t0+170*H)
    mk=candles('history-mark-price-candles',sym,t0,t0+170*H)
    out[sym]=(ix,mk)
pd.to_pickle(out,'index_all.pkl')
print(sum(v[0] is not None for v in out.values()),'with index of',len(out), '; mark', sum(v[1] is not None for v in out.values()))
