import pandas as pd, numpy as np
H=3600000; G0=pd.Timestamp('2021-12-01').value//10**6
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
tr=pd.read_csv(f'{SP}/results/okx_trades.csv'); ev=pd.read_parquet(f'{SP}/data/sim/events.parquet')
P=np.load(f'{SP}/data/sim/panel2.npz'); IA=pd.read_pickle('index_all.pkl')
# stop level per trade: shared stop from first open tranche
rows=[]
for sym,g in tr.groupby('sym'):
    i=int(ev.index[ev.sym==sym][0]); t0=int(ev.t0[i]); g0=(t0-G0)//H
    g=g.sort_values('tranche')
    sp={}
    for _,r in g.iterrows():
        sib=[sp[k] for k,rr in g.iterrows() if k in sp and rr.entry_t<=r.entry_t and rr.exit_t>r.entry_t and rr.reason!='x']
        sp[_]=sib[0] if sib else r.entry_px*1.5
    for kind,ref in zip(('index','mark'),IA[sym]):
        if ref is None: continue
        ref=ref.copy(); ref.index=(ref.index-t0)//H
        for k,r in g.iterrows():
            ke=int(r.entry_t-g0); kx=int(r.exit_t-g0); s=sp[k]
            if ke not in ref.index: continue
            rng=ref.loc[ke:min(kx,167) if r.reason=='stop' else 167]
            hit=rng[rng.h>=s]
            rows.append(dict(kind=kind,sym=sym,tr=r.tranche,reason=r.reason,ret=r.ret,kx=kx,entry_basis=P['o'][i,ke]/ref.o[ke]-1,
                             ref_hit_bar=int(hit.index.min()) if len(hit) else None,
                             ref_h_at_kx_vs_stop=ref.h.get(kx,np.nan)/s-1 if r.reason=='stop' else np.nan,
                             ref_maxh_vs_stop=rng.h.max()/s-1))
d=pd.DataFrame(rows); pd.set_option('display.width',250)
d.to_csv('idx_cmp.csv',index=False)
for kind in ('index','mark'):
    x=d[d.kind==kind]
    st=x[x.reason=='stop']; en=x[x.reason!='stop']
    print(kind,'trades',len(x),'| stops',len(st),'corroborated same bar',int((st.ref_hit_bar==st.kx).sum()),'earlier',int((st.ref_hit_bar<st.kx).sum()),'never',int(st.ref_hit_bar.isna().sum()))
    print('  end-trades where ref reached stop but last did not:',en[en.ref_hit_bar.notna()][['sym','tr','ret','ref_hit_bar','ref_maxh_vs_stop']].round(3).values.tolist())
    print('  entry basis last/ref-1: median %.4f  p5 %.4f p95 %.4f  max|.| %.4f'%(x.entry_basis.median(),x.entry_basis.quantile(.05),x.entry_basis.quantile(.95),x.entry_basis.abs().max()))
    print(st[['sym','tr','kx','ref_hit_bar','ref_h_at_kx_vs_stop']].round(3).to_string())
