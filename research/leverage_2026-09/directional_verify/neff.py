import numpy as np, pandas as pd
from scipy.stats import norm
from bt_copy import *
rets=[]; names=[]
for sym in ['BTCUSDT','ETHUSDT']:
    d=load(sym); n5=len(d); t=d.t.values; day=((t-t[0])//86400000).astype(np.int64)
    arrs=[d[k].values.astype(np.float64) for k in ['o','h','l','c','mo','mh','ml','fund']]; ff=d.fund_flag.values.astype(np.int8)
    vm,_=vol_mult(d, ms(IS0), ms(IS1))
    i0=int(np.searchsorted(t,ms(IS0))); i1=int(np.searchsorted(t,ms(IS1))); d0=int(day[i0]); nd=int(day[i1-1]-d0+1)
    for (tf,fam,p) in SIG_GRID:
        a,b_,c_,d_,atr,mh=signals(d,tf,fam,p)
        for sk in STOP_KINDS:
            stopf=np.full(n5,0.015) if sk=='fix1.5%' else np.nan_to_num(2*atr,nan=0.015)
            for tp in TP_KINDS:
                for sz in SIZING:
                    r=run_one(*arrs,ff,day,i0,i1,d0,nd,a,b_,c_,d_,stopf,vm,sz=='volscaled',1.0,tp,mh,True,False)
                    eq=np.concatenate([[1.0],r[0]]); rets.append(eq[1:]/eq[:-1]-1); names.append(f'{sym}|{tf}|{fam}|{p}|{sk}|{tp}|{sz}')
X=np.array(rets); sh=X.mean(1)/X.std(1)*np.sqrt(365)
C=np.corrcoef(X); ev=np.linalg.eigvalsh(C); ev=ev[ev>0]
p=ev/ev.sum(); neff_entropy=np.exp(-(p*np.log(p)).sum()); neff_pr=ev.sum()**2/(ev**2).sum()
print('configs',len(X),'max IS Sharpe', sh.max().round(3), names[sh.argmax()])
print('N_eff (entropy of eigenvalues) %.1f, N_eff (participation ratio) %.1f'%(neff_entropy,neff_pr))
def emax(N): return ((1-0.5772)*norm.ppf(1-1/N)+0.5772*norm.ppf(1-1/(N*np.e)))/np.sqrt(3.0)
for N in [288, neff_entropy, neff_pr]: print('E[max SR | null, N=%.0f] = %.2f'%(N, emax(N)))
# spread of IS Sharpes (empirical sd across configs vs theoretical 1/sqrt(3)=0.577)
print('cross-config IS Sharpe mean %.2f sd %.2f (null sd 0.58)'%(sh.mean(), sh.std()))
