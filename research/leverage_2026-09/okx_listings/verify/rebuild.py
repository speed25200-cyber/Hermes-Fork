"""Rebuild 1H OHLC for the 17 selected instruments from OKX daily trade archives (own download), compare with the
author's panel arrays hour by hour, and re-derive entry/exit/stop for the 20 selected trades."""
import sys, glob; sys.path.insert(0,'.')
from dl import *
import numpy as np
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
H=3600000; G0=pd.Timestamp('2021-12-01').value//10**6
sel=pd.read_csv('sel.csv')
ev=pd.read_parquet(f'{SP}/data/sim/events.parquet')
P=np.load(f'{SP}/data/sim/panel2.npz')
res=[]; bars={}
for inst in sel.sym.unique():
    fns=sorted(glob.glob(f'cache/okx/{inst}-*.zip'))
    fns=[f for f in fns if os.path.getsize(f)>0]
    tr=pd.concat([okx_read(f) for f in fns],ignore_index=True)
    tr=tr.drop_duplicates('trade_id').sort_values(['created_time','trade_id'])
    tr.to_parquet(f'cache/{inst}.trades.parquet')
    i=int(ev.index[ev.sym==inst][0]); t0=int(ev.t0[i])
    tr['k']=(tr.created_time-t0)//H
    g=tr.groupby('k').price
    b=pd.DataFrame({'o':g.first(),'h':g.max(),'l':g.min(),'c':g.last(),'n':g.size(),'vol':tr.groupby('k')['size'].sum()})
    bars[inst]=b
    first_trade=int(tr.created_time.min())
    K=np.arange(0,172)
    mine=b.reindex(K)
    au=pd.DataFrame({a:P[a][i,:172] for a in 'ohlc'},index=K)
    notrade=mine.o.isna()
    cmp=[]
    for a in 'ohlc':
        x=mine[a][~notrade]; y=au[a][~notrade]
        rd=(x/y-1).abs()
        cmp.append(rd.max())
    # gap-filled hours: author's value should equal previous close
    res.append(dict(inst=inst,src=sel.src[sel.sym==inst].iloc[0],first_trade_mine=pd.to_datetime(first_trade,unit='ms'),
                    t0_author=pd.to_datetime(int(ev.t0_exact[i]),unit='ms'),t0_diff_s=(first_trade-int(ev.t0_exact[i]))/1000,
                    hours_notrade_0_171=int(notrade.sum()),maxrel_o=cmp[0],maxrel_h=cmp[1],maxrel_l=cmp[2],maxrel_c=cmp[3],
                    au_nan_0_171=int(au.c.isna().sum()),ntr=len(tr)))
r=pd.DataFrame(res); pd.set_option('display.width',250)
print(r.to_string())
pd.to_pickle(bars,'okx_bars.pkl')
