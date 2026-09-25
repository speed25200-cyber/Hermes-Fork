"""Book + listing sleeve daily combination (main loop's recipe: weights = inverse IS vol over the 2023-08..2024-12
overlap, combined return = w_b r_book + w_l r_list, both scaled by L). Listing sleeve variants: the IS-picked config
(researcher's hybrid file, to reproduce the main loop), and the pre-registrable ensembles (hybrid2, from
ens_robust.py; scaled x2 so the short leg is at gross 1 like the main loop's sleeve). Also: the same with the listing
sleeve's OOS mean haircut to an honest forward Sharpe. Close-only daily drawdown (intrabar handled in the lens text).
-> combo.json"""
import json, numpy as np, pandas as pd
B = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv', index_col=0)
B.index = pd.to_datetime(B.index).tz_convert(None).normalize()
rb = B['return']
def sh(x):
    x = np.asarray(x, float); s = x.std(ddof=1); return float(x.mean() / s * np.sqrt(365)) if s > 0 else 0.0
def load(f, scale=1.0):
    r = pd.read_csv(f, index_col=0).iloc[:, 0]; r.index = pd.to_datetime(r.index); return r * scale
NL = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting/results/'
V = {'selected (main loop file, hybrid)': pd.concat([load(NL + 'is_daily_selected_hybrid.csv'), load(NL + 'oos_daily_selected_hybrid.csv')])}
import os
for nm, tag in (('IS top-10 ens (hybrid2)', 'IS_10'), ('plateau BTC-hedged 16 ens (hybrid2)', 'plateau_16'), ('plateau all 32 ens (hybrid2)', 'plateau_32')):
    if os.path.exists(f'oos_daily_{tag}.csv'):
        V[nm] = pd.concat([load(f'is_daily_{tag}.csv', 2.0), load(f'oos_daily_{tag}.csv', 2.0)])
out = {}
for nm, rl in V.items():
    rl = rl[~rl.index.duplicated()]
    idx = rb.index.intersection(rl.index)
    b, l = rb.reindex(idx), rl.reindex(idx)
    isw = slice('2023-08-01', '2024-12-31'); oos = slice('2025-01-01', '2026-08-31')
    vb, vl = b.loc[isw].std(), l.loc[isw].std()
    wb, wl = (1 / vb) / (1 / vb + 1 / vl), (1 / vl) / (1 / vb + 1 / vl)
    c = wb * b + wl * l
    o = dict(w_book=float(wb), w_list=float(wl), is_sharpe=sh(c.loc[isw]), oos_sharpe=sh(c.loc[oos]), corr_oos=float(b.loc[oos].corr(l.loc[oos])),
             list_oos_sharpe=sh(l.loc[oos]), book_oos_sharpe=sh(b.loc[oos]),
             years={str(y): float(np.prod(1 + v) - 1) for y, v in c.loc[oos].groupby(c.loc[oos].index.year)})
    m = c.loc[oos].index.to_period('M')
    o['lomo_min'] = min(sh(c.loc[oos][m != p]) for p in m.unique())
    # honest haircut: listing sleeve OOS mean scaled so its OOS Sharpe equals S_fwd (vol kept), book as is
    for S_fwd in (1.0, 0.7):
        lo = l.loc[oos]
        l2 = lo - lo.mean() + S_fwd * lo.std() / np.sqrt(365)
        c2 = wb * b.loc[oos] + wl * l2
        o[f'oos_sharpe_list_haircut_to_{S_fwd}'] = sh(c2)
        o[f'years_list_haircut_to_{S_fwd}'] = {str(y): float(np.prod(1 + v) - 1) for y, v in c2.groupby(c2.index.year)}
    lev = {}
    for L in (1, 2, 3, 4, 5):
        x = L * c.loc[oos]
        e = (1 + x).cumprod()
        lev[L] = dict(cagr=float(e.iloc[-1] ** (365 / len(x)) - 1), daily_maxdd=float((1 - e / e.cummax()).max()))
    o['lev_close_only'] = lev
    out[nm] = o
    print(nm, json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in o.items() if k != 'lev_close_only'}), {L: (round(v['cagr'], 3), round(v['daily_maxdd'], 3)) for L, v in lev.items()})
json.dump(out, open('combo.json', 'w'), indent=1)
