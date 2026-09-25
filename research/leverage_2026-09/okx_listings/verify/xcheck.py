import pandas as pd, numpy as np
H=3600000; G0=pd.Timestamp('2021-12-01').value//10**6
sel=pd.read_csv('sel.csv'); okx=pd.read_pickle('okx_bars.pkl'); by=pd.read_pickle('bybit_bars.pkl'); mi=pd.read_pickle('mark_index.pkl')
bmap={'MEMEFI-USDT-SWAP':('MEMEFIUSDT',1),'GRASS-USDT-SWAP':('GRASSUSDT',1),'FITFI-USDT-SWAP':('FITFIUSDT',1),'PYTH-USDT-SWAP':('PYTHUSDT',1),
   'ZEUS-USDT-SWAP':('ZEUSUSDT',1),'GPT-USDT-SWAP':('GPTUSDT',1),'PEPE-USDT-SWAP':('1000PEPEUSDT',1000),'GRIFFAIN-USDT-SWAP':('GRIFFAINUSDT',1),
   'MORPHO-USDT-SWAP':('MORPHOUSDT',1),'ZEREBRO-USDT-SWAP':('ZEREBROUSDT',1),'DUCK-USDT-SWAP':('DUCKUSDT',1),'NC-USDT-SWAP':('NCUSDT',1),
   'BUZZ-USDT-SWAP':('BUZZUSDT',1),'CP-USDT-SWAP':('CPUSDT',1),'DOS-USDT-SWAP':('DOSUSDT',1)}
def ref_bars(kind, inst, t0):
    if kind=='bybit':
        if inst not in bmap or bmap[inst][0] not in by: return None
        s,m=bmap[inst]; b=by[s].copy()
        for a in 'ohlc': b[a]=b[a]/m
        b.index=(b.index-t0)//H; return b
    d=mi[inst][kind]
    if d is None: return None
    d=d.copy(); d.index=(d.index-t0)//H; return d
rows=[]
for _,r in sel.iterrows():
    t0=int(r.t0); g0=(t0-G0)//H; ke=int(r.entry_t-g0); kx=int(r.exit_t-g0)
    # first tranche entry bar of this listing (stop reference): the stop was armed from the first entry
    rec=dict(sym=r.sym.replace('-USDT-SWAP',''),tr=r.tranche,reason=r.reason,ret_author=round(r.ret,4))
    for kind in ('mark','index','bybit'):
        b=ref_bars(kind,r.sym,t0)
        if b is None or ke not in b.index:
            rec[kind]='n/a'; continue
        e=b.o[ke]; rec[f'{kind}_entry_vs_okx']=round(e/r.entry_px-1,4)
        # stop scan on this reference from entry bar to exit bar (inclusive for stops, exclusive of 168 for 'end')
        # stop level: same fraction as author: stop_px relative to OKX first entry; on the reference, stop level = ref(first entry bar open)*1.5
        ke0=int(sel[(sel.sym==r.sym)].pipe(lambda x: x.entry_t.min())-g0) if r.tranche==0 else None
        rng=b.loc[ke:167]
        hit=rng[rng.h>=r.stop_px] if kind!='bybit' else rng[rng.h>=r.stop_px*(1+0)]
        rec[f'{kind}_first_stop_bar']=int(hit.index.min()) if len(hit) else None
        rec[f'{kind}_max_h_vs_stop']=round(rng.h.max()/r.stop_px-1,4) if len(rng) else None
        if r.reason=='stop':
            rec[f'{kind}_h_at_okx_stopbar_vs_stop']=round(b.h.get(kx,np.nan)/r.stop_px-1,4)
        else:
            rec[f'{kind}_exit_vs_okx']=round(b.o.get(kx,np.nan)/r.exit_px_implied-1,4) if kx in b.index else None
        # ret on this reference if its own stop triggered (fill at stop), else exit at its open of 168
        if len(hit):
            ret=-(max(b.o[hit.index.min()],r.stop_px)/r.entry_px-1)
        else:
            ret=-(b.o.get(168,np.nan)/r.entry_px-1) if 168 in b.index else np.nan
        rec[f'{kind}_ret_same_entry']=round(ret,4)
    rec['okx_stop_bar']=kx if r.reason=='stop' else None
    rows.append(rec)
d=pd.DataFrame(rows); pd.set_option('display.width',400); pd.set_option('display.max_columns',50)
d.to_csv('xcheck.csv',index=False)
for kind in ('mark','index','bybit'):
    cols=['sym','tr','reason','ret_author','okx_stop_bar']+[c for c in d.columns if c.startswith(kind)]
    print(d[cols].to_string()); print()
