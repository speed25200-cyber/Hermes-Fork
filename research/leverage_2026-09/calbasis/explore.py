import pandas as pd, numpy as np, glob, os
D='data'
def rd(n): 
    d=pd.read_parquet(f'{D}/{n}.parquet'); d.index=pd.to_datetime(d.t,unit='ms'); return d
for a in ['BTC','ETH']:
    p=rd(f'{a}USDT_perp_last'); f=rd(f'{a}USDT_funding')
    f.index=f.index.floor('h')
    print(a,'funding ann by year', (f.rate.groupby(f.index.year).mean()*3*365*100).round(2).to_dict())
    rows=[]
    for fn in sorted(glob.glob(f'{D}/{a}USDT_2*_last.parquet')):
        s=os.path.basename(fn).replace('_last.parquet',''); exp=pd.Timestamp('20'+s.split('_')[1])+pd.Timedelta(hours=8)
        q=rd(s+'_last'); q=q[q.index<exp]
        j=q[['c','qv']].join(p[['c']],rsuffix='_p',how='inner')
        tau=(exp-j.index).total_seconds()/86400
        prem=j.c/j.c_p-1
        ann=prem*365/tau
        d=pd.DataFrame({'prem':prem,'ann':ann,'tau':tau,'qv':j.qv})
        dd=d.resample('1D').agg({'prem':'last','ann':'last','tau':'last','qv':'sum'})
        dd=dd[dd.tau>7]
        # funding realized during contract life after each day
        rows.append((s, str(q.index[0])[:10], round(dd.ann.median()*100,2), round(dd.ann.quantile(.1)*100,2), round(dd.ann.quantile(.9)*100,2), round(dd.qv.median()/1e6,1), round(dd.prem.abs().max()*100,2)))
    print(pd.DataFrame(rows,columns=['sym','start','ann_med%','ann_p10','ann_p90','dailyQV_M$','maxabsprem%']).to_string())
