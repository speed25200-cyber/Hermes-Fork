"""Largest intrabar spread shocks of the classic pairs (1-minute conservative bound, Binance 1m mark prices), and the
per-leg leverage that a single-pair cross account could have survived.

For each fixed pair and month, beta from the walk-forward formation ('fixed', 'ret', Wf=120).  For each hour,
WL = worst long-spread move and WS = worst short-spread move (per unit of A notional; minute_bound.py).
Single pair in a cross account, per-leg leverage L (nA = 2LE/(1+beta), nB = beta nA): liquidation when
E + nA*w <= nA(mmrA+fee) + nB(mmrB+fee)  <=>  L >= 1 / ( MMfac + 2|w|/(1+beta) ),
MMfac = 2((mmrA+fee) + beta(mmrB+fee))/(1+beta).
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *

P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
key = ('fixed', 'ret', 120)
pm = sorted(all_pair_months({key: sel[key]}))
bounds = minute_bound.compute_bounds(pm, list(P['syms']), P['o'].astype(np.float64), GRID, FORM_DATES,
                                     log=lambda s: None)
rows = []
for (t, a, b, be), (wl, ws) in bounds.items():
    i0 = GRID.get_loc(t)
    for d, w in (('long', wl), ('short', ws)):
        if np.isnan(w).all():
            continue
        j = int(np.nanargmin(w))
        rows.append(dict(pair=f'{syms[a]}/{syms[b]}', month=str(t.date())[:7], dir=d, beta=be, worst=float(w[j]),
                         when=str(GRID[i0 + j]), p999=float(np.nanpercentile(w, 0.1)),
                         mma=float(mmr[a]), mmb=float(mmr[b])))
df = pd.DataFrame(rows)
mmfac = 2 * ((df.mma + FEE_T) + df.beta * (df.mmb + FEE_T)) / (1 + df.beta)
df['lmax'] = 1.0 / (mmfac + 2 * (-df.worst) / (1 + df.beta))
agg = df.sort_values('worst').groupby('pair').head(3)[['pair', 'dir', 'beta', 'worst', 'when', 'lmax']]
print(agg.round(4).to_string(index=False))
per = df.groupby('pair').agg(worst=('worst', 'min'), lmax_min=('lmax', 'min'),
                             months_lmax_below_10=('lmax', lambda x: int((x < 10).sum())),
                             months_lmax_below_5=('lmax', lambda x: int((x < 5).sum())), months=('month', 'nunique'))
print(per.round(3).to_string())
per.to_csv(os.path.join(OUT, 'shock_fixed_pairs.csv'))
agg.to_csv(os.path.join(OUT, 'shock_fixed_worst.csv'), index=False)
