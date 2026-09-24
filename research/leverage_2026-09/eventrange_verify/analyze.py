"""Summaries of the pre-registered grids: distribution per family x L x period, IS-selected configs (S1, S2), zero-cost
and no-range-slippage sensitivities. Writes out/summary_distribution.csv, out/selected.csv, out/results.json."""
import json, os, sys
import numpy as np, pandas as pd
from scipy.stats import spearmanr
HERE = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(HERE, 'out')
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40); pd.set_option('display.max_colwidth', 120)
df = pd.read_csv(os.path.join(OUT, 'grid_wick_grid_orb.csv'))
piv = df.pivot_table(index=['family', 'cfg_id', 'L'], columns='period', values=['cagr', 'maxdd', 'liq', 'sharpe', 'trades', 'worst_day'])
piv.columns = [f'{a}_{b}' for a, b in piv.columns]
piv = piv.reset_index()
cfgs = df.drop_duplicates(['family', 'cfg_id']).set_index(['family', 'cfg_id'])['cfg']
# ---- distribution
dist = piv.groupby(['family', 'L']).apply(lambda g: pd.Series(dict(
    n=len(g),
    IS_pos=int((g.cagr_IS > 0).sum()), OOS_pos=int((g.cagr_OOS > 0).sum()), both_pos=int(((g.cagr_IS > 0) & (g.cagr_OOS > 0)).sum()),
    OOS_med=g.cagr_OOS.median(), OOS_p90=g.cagr_OOS.quantile(0.9), OOS_max=g.cagr_OOS.max(), IS_max=g.cagr_IS.max(),
    liq_any_IS=int((g.liq_IS > 0).sum()), liq_any_OOS=int((g.liq_OOS > 0).sum()),
    frac_liq_OOS=(g.liq_OOS > 0).mean(), frac_liq_either=((g.liq_IS > 0) | (g.liq_OOS > 0)).mean(),
    ruined_OOS=int((g.cagr_OOS <= -0.99).sum()), dd50_OOS=int((g.maxdd_OOS >= 0.5).sum()),
    spearman_IS_OOS=spearmanr(g.cagr_IS, g.cagr_OOS).correlation)), include_groups=False).reset_index()
print(dist.round(3).to_string())
dist.to_csv(os.path.join(OUT, 'summary_distribution.csv'), index=False)
# ---- S1: best IS CAGR per family and L
full = df.set_index(['family', 'cfg_id', 'L', 'period'])
sel = []
for (fam, L), g in piv.groupby(['family', 'L']):
    b = g.sort_values('cagr_IS', ascending=False).iloc[0]
    for per in ('IS', 'OOS'):
        r = full.loc[(fam, int(b.cfg_id), L, per)]
        sel.append(dict(rule='S1_bestIS_CAGR_at_L', family=fam, L=L, period=per, cfg_id=int(b.cfg_id), cfg=cfgs[(fam, int(b.cfg_id))],
                        cagr=r.cagr, maxdd=r.maxdd, worst_day=r.worst_day, worst_intraday=r.worst_intraday, sharpe=r.sharpe, liq=r.liq,
                        trades=r.trades, per_year=r.per_year, coin_cagr=r.coin_cagr))
# ---- S2: best IS Sharpe at L=1, all L; chosen L = argmax IS CAGR
for fam, g in piv[piv.L == 1].groupby('family'):
    b = g.sort_values('sharpe_IS', ascending=False).iloc[0]
    ci = int(b.cfg_id)
    Ls = piv[(piv.family == fam) & (piv.cfg_id == ci)].set_index('L')
    Lstar = int(Ls.cagr_IS.idxmax())
    for L in Ls.index:
        for per in ('IS', 'OOS'):
            r = full.loc[(fam, ci, L, per)]
            sel.append(dict(rule='S2_bestIS_Sharpe_L1' + ('_chosenL' if L == Lstar else ''), family=fam, L=L, period=per, cfg_id=ci, cfg=cfgs[(fam, ci)],
                            cagr=r.cagr, maxdd=r.maxdd, worst_day=r.worst_day, worst_intraday=r.worst_intraday, sharpe=r.sharpe, liq=r.liq,
                            trades=r.trades, per_year=r.per_year, coin_cagr=r.coin_cagr))
sel = pd.DataFrame(sel)
sel.to_csv(os.path.join(OUT, 'selected.csv'), index=False)
print(sel.drop(columns=['coin_cagr']).round(4).to_string())
