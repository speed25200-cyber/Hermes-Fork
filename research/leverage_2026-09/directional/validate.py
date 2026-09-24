import numpy as np, pandas as pd
from bt import *
d = load('BTCUSDT'); t = d.t.values; n5 = len(d)
day = ((t - t[0]) // 86400000).astype(np.int64)
arrs = [d[k].values.astype(np.float64) for k in ['o','h','l','c','mo','mh','ml','fund']]
ff = d.fund_flag.values.astype(np.int8)
i0 = int(np.searchsorted(t, ms(IS0))); i1 = int(np.searchsorted(t, ms(IS1)))
d0 = int(day[i0]); nd = int(day[i1-1]-d0+1)
evL, evS, exL, exS, atr, mh = signals(d, '4h', 'ema', (20,100))
nostop = np.full(n5, 10.0)
vm = np.ones(n5)
res = run_one(*arrs, ff, day, i0, i1, d0, nd, evL, evS, exL, exS, nostop, vm, False, 1.0, 0.0, 0, True, True)
print('sim final', res[0][-1], 'trades', res[2], 'liq', res[3], 'stops', res[4])
# vectorized reference: position set at 5m open after 4h close, no stops, 1x, costs per switch; funding
m = 48
b = tf_frame(d, m); c = b.c
diff = c.ewm(span=20, adjust=False).mean() - c.ewm(span=100, adjust=False).mean()
sig = np.sign(diff).values
pos = to_grid_ffill(sig.astype(float), m, n5)  # position held from open of index (k+1)m
o = d.o.values; cl = d.c.values
# position during bar i = pos[i]; but the sim only enters on a cross event inside the window
pos = np.nan_to_num(pos)
# find first cross event at/after i0: before that the sim is flat
first = i0 + np.argmax((evL[i0:i1] | evS[i0:i1]) == 1)
pw = pos[i0:i1].copy(); pw[:first - i0] = 0
# per-bar returns: open-to-open
oo = np.append(o[i0+1:i1], cl[i1-1]) / o[i0:i1] - 1
eq = 1.0; prev = 0.0
# simulate on open-to-open with costs at switches (each switch: exit + entry costs)
r = pw * oo
chg = np.abs(np.diff(np.concatenate([[0], pw, [0]])))  # 1 for enter/exit, 2 for flip
cost = chg[:-1] * (FEE_T + SLIP)
fund = d.fund.values[i0:i1] * d.fund_flag.values[i0:i1]
fcost = pw * fund  # approx (notional ~ equity)
# log-equity approx (1x): compound per-bar returns (the sim holds a fixed quantity per trade, small difference)
eqv = np.prod(1 + r - cost - fcost) * (1 - chg[-1] * (FEE_T + SLIP))
print('vectorized approx final', eqv, 'switch events', int((chg[:-1] > 0).sum()))
tr = res[8]
print('trade ret stats: n', len(tr), 'mean', tr.mean(), 'min', tr.min(), 'max', tr.max())
# buy and hold
print('BTC B&H IS', cl[i1-1]/o[i0], 'OOS', cl[-1]/o[i1])
# trade-level reference
idx = np.where(((evL | evS) == 1))[0]; idx = idx[(idx >= i0) & (idx < i1)]
fr = d.fund.values; ffl = d.fund_flag.values; mo = d.mo.values
eq = 1.0; rets = []
for k, a in enumerate(idx):
    dd = 1 if evL[a] == 1 else -1
    b_ = idx[k+1] if k + 1 < len(idx) else None
    P0 = o[a] * (1 + SLIP * dd)
    if b_ is None:
        P1 = cl[i1-1] * (1 - SLIP * dd); endi = i1
    else:
        P1 = o[b_] * (1 - SLIP * dd); endi = b_
    Q = eq / P0
    fsum = sum(dd * fr[j] * Q * mo[j] for j in range(a + 1, endi + 1) if j < i1 and ffl[j] == 1 and j != a)
    # sim charges funding at bars a+1 .. exit bar (inclusive, funding before exit at same open)
    new = eq - FEE_T * eq + dd * Q * (P1 - P0) - FEE_T * Q * P1 - fsum
    rets.append(new / eq - 1); eq = new
print('trade-level reference final', eq, 'n', len(rets))
print('max abs diff in trade rets', np.max(np.abs(np.array(rets) - tr)))
