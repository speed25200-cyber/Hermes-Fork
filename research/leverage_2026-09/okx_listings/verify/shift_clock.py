"""Timing sensitivity: live computes start = first-1m-candle + 24 h and enters at the first decision time >= start,
whereas the sim floors t0 to the hour. Upper-bound check: for events whose launch minute is not HH:00, move the local
clock one hour later (arrays shifted left by one hour, t0 += 1 h). Built on the corrected-funding panel."""
import os, numpy as np, pandas as pd, shutil
V=os.path.dirname(os.path.abspath(__file__)); H=3600000
src=os.path.join(V,'sim_fix'); out=os.path.join(V,'sim_fix_shift'); os.makedirs(out,exist_ok=True)
ev=pd.read_parquet(os.path.join(src,'events.parquet')); P=dict(np.load(os.path.join(src,'panel2.npz')))
m=((ev.t0_exact//60000)*60000)!=ev.t0
print('events with launch minute != HH:00:', int(m.sum()), 'of', len(ev))
for k in ('o','h','l','c','bo','bh','bl','bc','fund','fund_okx','okx_on'):
    A=P[k].copy()
    fill=False if A.dtype==bool else (0.0 if k=='fund' else np.nan)
    A[m.values,:-1]=P[k][m.values,1:]; A[m.values,-1]=fill
    P[k]=A
ev.loc[m,'t0']=ev.loc[m,'t0']+H
np.savez_compressed(os.path.join(out,'panel2.npz'),**P); ev.to_parquet(os.path.join(out,'events.parquet'))
