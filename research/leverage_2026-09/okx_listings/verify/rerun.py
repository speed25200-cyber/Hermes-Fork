"""Re-run the author's run.py / combo.py logic (frozen live rule) on the author's panel and on corrected-funding panels."""
import sys, json, types
SP="/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP+'/review_sleeve'); sys.path.insert(0, SP+'/newlisting')
import sim
import numpy as np, pandas as pd
from livesim import simulate_live, sharpe
V=SP+'/xlist/verify'; END='2026-09-25'; H=240; SREF=0.1238230231575359
def tstat(x):
    x=np.asarray(x,float); return float(x.mean()/x.std(ddof=1)*np.sqrt(len(x))) if len(x)>2 else float('nan')
def load(d):
    sim.D=d; D=sim.Data('binance'); D.sigma_ref=SREF; return D
def run(D, start, end, **kw):
    r=simulate_live(D,tranches=(24,72),d1=168,stop=0.5,K=5,late=True,max_late=2,shared_stop=True,start=start,end=end,**kw)
    tr=pd.DataFrame(r['trades']); ret=r['ret']
    return dict(sharpe=round(sharpe(ret),3),n=len(tr),mean=round(float(tr.ret.mean()),4),t=round(tstat(tr.ret),2),maxdd=round(r['maxdd'],4),
                years={int(k):round(float(np.prod(1+v)-1),4) for k,v in ret.groupby(ret.index.year)}), tr
out={}
panels={'author':SP+'/xlist/data/sim','fix_funding':V+'/sim_fix','fix_funding_norounding':V+'/sim_fix_norm'}
for extra in sys.argv[1:]:
    k,p=extra.split('='); panels[k]=p
for name,d in panels.items():
    D=load(d)
    for pn,a,b in (('2022-2026','2022-01-01',END),('2022-2024','2022-01-01','2025-01-01'),('2025-2026','2025-01-01',END),('2026','2026-01-01',END)):
        rec,tr=run(D,a,b); out[f'{name} {pn}']=rec
        print(f'{name:24s} {pn:10s} Sharpe {rec["sharpe"]:.3f} n {rec["n"]} mean {rec["mean"]:+.4f} t {rec["t"]} maxDD {rec["maxdd"]:.3f} {rec["years"]}',flush=True)
# union (combo.py logic) with author vs fixed OKX panel
Db=sim.Data.__new__(sim.Data)
sim.D=SP+'/newlisting/data'; Db=sim.Data('hybrid'); Db.sigma_ref=SREF
def merge(a,b,keep_b=None):
    m=types.SimpleNamespace(); kb=np.ones(b.n,bool) if keep_b is None else keep_b
    for k in ('o','h','l','c','bo','bh','bl','bc','fund','okx_on'): m.__dict__[k]=np.concatenate([getattr(a,k)[:,:H],getattr(b,k)[kb,:H]])
    for k in ('vol_d','vol_n'): m.__dict__[k]=np.concatenate([getattr(a,k)[:,:H-1],getattr(b,k)[kb,:H-1]])
    for k in ('g0','newtok','year'): m.__dict__[k]=np.concatenate([getattr(a,k),getattr(b,k)[kb]])
    m.ev=pd.concat([a.ev[['sym','t0']].assign(src='binance'),b.ev[['sym','t0']].assign(src='okx')[kb]],ignore_index=True)
    m.n,m.H=len(m.ev),H; m.sigma_ref=SREF; return m
for name in ('author','fix_funding'):
    Do=load(panels[name]); U=merge(Db,Do)
    for pn,a,b in (('2022-2024','2022-01-01','2025-01-01'),('2025-2026','2025-01-01','2026-09-01'),('2026 Jan-Aug','2026-01-01','2026-09-01'),('2022-2026','2022-01-01','2026-09-01')):
        r=simulate_live(U,tranches=(24,72),d1=168,stop=0.5,K=5,late=True,max_late=2,shared_stop=True,start=a,end=b)
        rec=dict(sharpe=round(sharpe(r['ret']),3),maxdd=round(r['maxdd'],3),trades=len(r['trades'])); out[f'union {name} {pn}']=rec
        print(f'union {name:12s} {pn:13s} Sharpe {rec["sharpe"]:.3f} maxDD {rec["maxdd"]:.3f} trades {rec["trades"]}',flush=True)
json.dump(out,open(V+'/rerun.json','w'),indent=1)
