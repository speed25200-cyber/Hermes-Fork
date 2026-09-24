import pandas as pd, numpy as np
D='data'
def rd(n):
    d=pd.read_parquet(f'{D}/{n}.parquet'); d.index=pd.to_datetime(d.t,unit='ms'); return d
for s in ['BTCUSDT_240927','BTCUSDT_240628','BTCUSDT_250627','ETHUSDT_220930']:
    a=s.split('_')[0]
    p=rd(f'{a}_perp_last'); q=rd(s+'_last'); m=rd(s+'_mark'); pm=rd(f'{a}_perp_mark')
    j=pd.DataFrame({'F':q.c,'P':p.c,'Fm':m.c,'Pm':pm.c,'qv':q.qv}).dropna()
    j['prem']=(j.F/j.P-1)*100; j['premm']=(j.Fm/j.Pm-1)*100
    i=j.prem.abs().idxmax()
    print(s, i); print(j.loc[i-pd.Timedelta('30min'):i+pd.Timedelta('30min')].round(3).to_string())
