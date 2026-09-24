import sys, numpy as np, pandas as pd
from common import load, minute_of, NMIN
sym = sys.argv[1]
X = load(sym)
b = X['b_c'].copy()
ok = X['s_ok'] & X['f_ok'] & np.isfinite(b)
b[~ok] = np.nan
s = pd.Series(b)
for W in [240, 1440]:
    med = s.shift(1).rolling(W, min_periods=W//2).median().values
    d = b - med
    i0, i1, i2 = minute_of('2022-01-01'), minute_of('2025-01-01'), NMIN
    print(f'--- {sym} W={W}  quantiles of dev (bp), IS')
    dd = d[i0:i1]; dd = dd[np.isfinite(dd)]
    print(' '.join(f'{q}:{np.quantile(dd,q)*1e4:.1f}' for q in [0.0001,0.001,0.01,0.05,0.5,0.95,0.99,0.999,0.9999]))
    for th in [10e-4, 20e-4, 30e-4, 50e-4]:
        for side in [1, -1]:
            for (a, z, lab) in [(i0, i1, 'IS'), (i1, i2, 'OOS')]:
                dv = d[a:z]; bo = X['b_o'][a:z]
                sig = np.where(side * dv > th)[0]
                if len(sig) == 0: print(th, side, lab, 0); continue
                # first minute of each episode
                ep = sig[np.r_[True, np.diff(sig) > 30]]
                res = []
                for h in [1, 5, 15, 60, 240]:
                    j = ep + h
                    j = j[j < len(dv)]
                    # change of basis from next-open entry to close at t+h, in favour of the trade (short basis if side=1)
                    ent = bo[ep[:len(j)] + 1] if True else None
                    chg = side * (ent - b[a:z][j])
                    res.append(np.nanmean(chg) * 1e4)
                print(f'th={th*1e4:.0f}bp side={side:+d} {lab}: minutes={len(sig)} episodes={len(ep)}  mean gain vs next-open entry (bp) h=1,5,15,60,240: ' + ' '.join(f'{r:.1f}' for r in res))
