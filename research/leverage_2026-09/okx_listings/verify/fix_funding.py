"""Corrected funding for the OKX-listing events -> verify/sim_fix*/ (copies of the author's events.parquet + panel2.npz
with only 'fund' replaced).
 variant 'fix'  : settlement times = daily all-swaps label - 8 h (fixed; validated against per-instrument monthly files),
                  per-instrument monthly files where downloaded, author's post-2025-09 rows, + paginated REST history for
                  the 2026-09 events whose first days were missing; times rounded to the hour (settlement at HH:00:0x
                  belongs to the position held at HH:00).
 variant 'fix_norm': same, without the hour rounding (isolates the 4 h-lag bug)."""
import os, shutil, numpy as np, pandas as pd
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
V=os.path.dirname(os.path.abspath(__file__)); H=3600000
ev=pd.read_parquet(f'{SP}/data/sim/events.parquet')
raw=pd.read_parquet(f'{SP}/../xvenue/data/okx_funding_all.parquet')
raw=raw[raw.instId.isin(ev.sym)][['instId','funding_time','real_funding_rate']].rename(columns={'real_funding_rate':'rate'})
raw['funding_time']=raw.funding_time-8*H
mon=pd.read_parquet(os.path.join(V,'fund_monthly.parquet'))[['instId','funding_time','rate']]
au=pd.read_parquet(f'{SP}/data/ev_funding.parquet')
cut=pd.Timestamp('2025-09-07 14:00').value//10**6         # last settlement covered by the daily files after the -8 h shift
au_post=au[au.funding_time>cut]
rest=pd.read_parquet(os.path.join(V,'fund_rest.parquet'))[['instId','funding_time','rate']]
def rnd(s): return ((s+H//2)//H)*H
parts=[]
for sym in ev.sym:
    m=mon[mon.instId==sym]; r=raw[raw.instId==sym]
    # prefer monthly (truth) inside its span, raw-8h outside it, author post-cut rows + REST for the rest
    if len(m):
        lo,hi=rnd(m.funding_time).min(),rnd(m.funding_time).max()
        r=r[(rnd(r.funding_time)<lo)|(rnd(r.funding_time)>hi)]
    x=pd.concat([m,r,au_post[au_post.instId==sym],rest[rest.instId==sym]])
    x=x.assign(th=rnd(x.funding_time)).drop_duplicates(['instId','th'],keep='first')
    parts.append(x)
fx=pd.concat(parts,ignore_index=True)
fx.to_parquet(os.path.join(V,'ev_funding_fixed.parquet'))
P=dict(np.load(f'{SP}/data/sim/panel2.npz'))
for name,use_round in (('sim_fix',True),('sim_fix_norm',False)):
    out=os.path.join(V,name); os.makedirs(out,exist_ok=True)
    F=np.zeros_like(P['fund'])
    for i,r in ev.iterrows():
        ff=fx[fx.instId==r.sym]
        t=ff.th.values if use_round else ff.funding_time.values
        idx=np.ceil((t-r.t0)/H).astype(np.int64)-1
        ok=(idx>=0)&(idx<F.shape[1]); np.add.at(F[i],idx[ok],ff.rate.values[ok])
    Q=dict(P); Q['fund']=F
    np.savez_compressed(os.path.join(out,'panel2.npz'),**Q)
    shutil.copy(f'{SP}/data/sim/events.parquet',os.path.join(out,'events.parquet'))
    d=F[:,24:168]-P['fund'][:,24:168]
    print(name,'sum|diff| hours 24-168',round(float(np.abs(d).sum()),4),'events changed',int((np.abs(d).sum(1)>1e-12).sum()))
