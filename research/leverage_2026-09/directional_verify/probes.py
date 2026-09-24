import pandas as pd, numpy as np, json
from indep_sim import *
rows=[]
df,_=load_raw('ETHUSDT'); sig=make_signals(df,'4h','ema',(20,100))
variants={
 'base':{},
 'delay_5m':dict(delay=1),
 'delay_1h':dict(delay=12),
 'opt_ordering':dict(ordering='opt'),
 'liq_keeps_MM_equity':dict(liq_leaves_mm=True),
 'all_maker_no_slip':dict(taker=0.0002, slip=0.0, stop_slip=0.0),   # entries/exits as maker (optimistic), stops still hit
 'harsher_stop_slip':dict(stop_slip=0.0005, stop_impact=0.5),
 'mmr_1pct':dict(mmr=0.01),
}
for name,kw in variants.items():
    for win in ['IS','OOS']:
        for L in [1,5,10,15,20]:
            r=simulate(df,sig,win,L,stop_kind='fix',tp_R=2.0,**kw)
            rows.append(dict(variant=name,win=win,L=L,cagr=round(r['cagr'],4),final=r['final'],maxdd=round(r['maxdd'],4),liq=r['liq'],trades=r['trades']))
R=pd.DataFrame(rows)
print(R.pivot_table(index=['variant','win'],columns='L',values='cagr').to_string())
print(R.pivot_table(index=['variant','win'],columns='L',values='liq').to_string())
R.to_csv(f'{OUT}/probes_pickA_ETH.csv',index=False)
