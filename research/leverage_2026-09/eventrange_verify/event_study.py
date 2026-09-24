"""Event study for the wick/cascade family (no stops, no targets): signed forward return in the FADE direction from the
next bar's open to the close k minutes later, for non-overlapping events (>= 240 min apart per coin).
Reported gross (no costs) in basis points of price, IS and OOS separately, pooled over the 7 coins.
Round-trip cost hurdle at VIP0 with taker in/out ~ 10 bp fees + 2 x slippage (1-5 bp base + fast-market add-on)."""
import numpy as np, pandas as pd, itertools, json
from common import *
from signals import wick_events
H = [1, 5, 15, 60, 240]
rows = []
D = {c: load(c) for c in COINS}
for w, z, V in itertools.product((1, 5), (6, 10), (3, 10)):
    for per, (a, b) in dict(IS=(IS0, IS1), OOS=(OOS0, OOS1)).items():
        acc = {k: [] for k in H}; accL = {k: [] for k in H}; accS = {k: [] for k in H}
        for coin in COINS:
            d = D[coin]; i0, i1 = window(d, a, b)
            idx, dr, mf = wick_events(d, w, z, V)
            m = (idx >= i0) & (idx < i1 - 241)
            idx, dr = idx[m], dr[m]
            keep = []; last = -10**9
            for j, i in enumerate(idx):
                if i - last >= 240:
                    keep.append(j); last = i
            idx, dr = idx[keep], dr[keep]
            ent = d['o'][idx + 1]
            for k in H:
                fwd = d['c'][idx + k] / ent - 1
                acc[k].append(dr * fwd)
                accL[k].append((dr * fwd)[dr == 1]); accS[k].append((dr * fwd)[dr == -1])
        row = dict(w=w, z=z, V=V, period=per, n=int(sum(len(x) for x in acc[1])))
        for k in H:
            x = np.concatenate(acc[k])
            row[f'bp_{k}m'] = round(1e4 * x.mean(), 2); row[f't_{k}m'] = round(x.mean() / x.std() * np.sqrt(len(x)), 2)
            xl = np.concatenate(accL[k]); xs = np.concatenate(accS[k])
            row[f'bp_{k}m_long'] = round(1e4 * xl.mean(), 2); row[f'bp_{k}m_short'] = round(1e4 * xs.mean(), 2)
        rows.append(row)
df = pd.DataFrame(rows)
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40)
print(df[['w', 'z', 'V', 'period', 'n'] + [f'bp_{k}m' for k in H] + [f't_{k}m' for k in H]].to_string())
print(df[['w', 'z', 'V', 'period'] + [f'bp_{k}m_long' for k in H] + [f'bp_{k}m_short' for k in H]].to_string())
df.to_csv('out/wick_event_study.csv', index=False)
