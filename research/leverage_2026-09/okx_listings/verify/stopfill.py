import pandas as pd, numpy as np
H=3600000; G0=pd.Timestamp('2021-12-01').value//10**6
sel=pd.read_csv('sel.csv'); tr_all=pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist/results/okx_trades.csv')
st=tr_all[tr_all.reason=='stop'].copy()
first=tr_all.sort_values(['i','tranche']).groupby('i').entry_px.first()
st['stop_px']=st.i.map(first)*1.5
import os
rows=[]
for sym,g in st.groupby('sym'):
    fn=f'cache/{sym}.trades.parquet'
    if not os.path.exists(fn): continue
    t=pd.read_parquet(fn,columns=['created_time','price','size'])
    r=g.iloc[0]; sp=r.stop_px
    ev_t0=int(sel[sel.sym==sym].t0.iloc[0]); g0=(ev_t0-G0)//H; kx=int(r.exit_t-g0)
    s=t[(t.created_time>=ev_t0+kx*H)&(t.created_time<ev_t0+(kx+1)*H)]
    c=s[s.price>=sp].created_time.min()
    after=s[s.created_time>=c]
    def px_at(sec): 
        x=after[after.created_time<=c+sec*1000]; return x.price.iloc[-1]
    def vwap(sec):
        x=after[after.created_time<=c+sec*1000]; return (x.price*x['size']).sum()/x['size'].sum()
    rows.append(dict(sym=sym,stop_bar=kx,sim_fill_vs_stop=max(s.price.iloc[0],sp)/sp-1,px_1s=px_at(1)/sp-1,px_5s=px_at(5)/sp-1,px_30s=px_at(30)/sp-1,px_60s=px_at(60)/sp-1,
                     vwap_5s=vwap(5)/sp-1,vwap_60s=vwap(60)/sp-1,min_after_60s=after[after.created_time<=c+60000].price.min()/sp-1))
d=pd.DataFrame(rows).round(4); pd.set_option('display.width',250); print(d.to_string()); print(d.drop(columns=['sym','stop_bar']).mean().round(4))
d.to_csv('stopfill.csv',index=False)
