import time, pickle, os
import numpy as np, pandas as pd
import cb_backtest as cb
t=time.time()
if os.path.exists('assets.pkl'): AS=pickle.load(open('assets.pkl','rb'))
else:
    AS={a:cb.load_asset(a) for a in ['BTC','ETH']}; pickle.dump(AS,open('assets.pkl','wb'))
print('load',time.time()-t)
for a in ['BTC','ETH']:
    for c in AS[a]['cons']: 
        if c['eb']>=0: print(a,c['name'],c['exp'],'settle',round(c['settle'],2),'lastF',c['F'][-1], 'P',AS[a]['P'][c['eb']])
p=dict(signal='raw',h_in=0.08,h_out=None,tau_min=14)
for mode in ['cross','isolated','isolated_rb']:
  for L in [1,5,10,20]:
    t=time.time()
    s,m=cb.run_one(AS['BTC'],p,L,mode,'2022-01-01','2024-12-31 23:55')
    print(mode,L,{k:(round(v,4) if isinstance(v,float) else v) for k,v in m.items() if k!='years'}, round(time.time()-t,2))
tr=pd.DataFrame(cb.Sim(AS['BTC'],p,1,'cross',start='2022-01-01',end='2024-12-31 23:55').run().trades)
print(tr.to_string())
