"""(c) Hedge variants BTC vs EW alt index vs none (same configs, same sim), selected + ensembles; event-level alt beta
regression of the raw coin return (entry->day 7) on BTC and alt index over the same window. -> hedge.json"""
import json, numpy as np, pandas as pd
R = pd.read_parquet('daily_all.parquet')
def sh(x):
    x = np.asarray(x, float); s = x.std(ddof=1); return float(x.mean() / s * np.sqrt(365)) if s > 0 else 0.0
def yrs(r):
    return {str(k): round(float(np.prod(1 + v) - 1), 3) for k, v in r.groupby(r.index.year)}
def lomo(r):
    m = r.index.to_period('M'); return min(sh(r[m != p]) for p in m.unique())
def members(d0s, stops, unis, hedge):
    out = []
    for d0 in d0s:
        for st in stops:
            for u in unis:
                b = 0 if hedge == 'none' else 1
                h = 'alt' if hedge == 'alt' else 'btc'
                out.append(f"s-1_d{d0}_e7_b{b}_st{st}_{u}_{h}")
    return out
sets = {'selected (3d->7d, stop .5, newtok)': ([72], [0.5], ['newtok']),
        'plateau (1d,3d)x(4 stops)x(2 uni)': ([24, 72], [0, 0.25, 0.5, 1.0], ['okx', 'newtok']),
        'plateau newtok only': ([24, 72], [0, 0.25, 0.5, 1.0], ['newtok']),
        '1d->7d, all stops/uni': ([24], [0, 0.25, 0.5, 1.0], ['okx', 'newtok']),
        '3d->7d, all stops/uni': ([72], [0, 0.25, 0.5, 1.0], ['okx', 'newtok'])}
out = {}
for nm, (d0s, st, un) in sets.items():
    out[nm] = {}
    for h in ('btc', 'alt', 'none'):
        cols = members(d0s, st, un, h)
        r = R[cols].mean(axis=1)
        i, o = r.loc[:'2024-12-31'], r.loc['2025-01-01':]
        out[nm][h] = dict(is_sharpe=sh(i), oos_sharpe=sh(o), oos_lomo=lomo(o), years=yrs(r), oos_vol=float(o.std() * np.sqrt(365)))
        print(f"{nm:40s} {h:5s} IS {sh(i):.2f} OOS {sh(o):.2f} lomo {lomo(o):.2f} vol {o.std()*np.sqrt(365):.3f} {yrs(r)}")
# daily beta of the unhedged plateau ensemble to BTC / alt index daily returns
import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
A = pd.read_parquet('altidx_d.parquet').close.pct_change()
X = pd.read_parquet('events_d7.parquet')
reg = {}
for d0 in (24, 72):
    for per, m in (('IS', X.t < '2025-01-01'), ('OOS', X.t >= '2025-01-01'), ('ALL', X.t.notna())):
        Y = X[(X.d0 == d0) & m]
        for f in ('btc', 'alt'):
            Z = np.column_stack([np.ones(len(Y)), Y[f].values])
            coef, res, *_ = np.linalg.lstsq(Z, Y.coin.values, rcond=None)
            e = Y.coin.values - Z @ coef
            se = np.sqrt(np.diag(np.linalg.inv(Z.T @ Z)) * e.var(ddof=2))
            reg[f'd0={d0}_{per}_{f}'] = dict(n=len(Y), alpha=float(coef[0]), alpha_t=float(coef[0] / se[0]), beta=float(coef[1]), beta_t=float(coef[1] / se[1]),
                                             mean_factor_ret=float(Y[f].mean()), beta_contrib=float(coef[1] * Y[f].mean()), mean_coin=float(Y.coin.mean()))
            print(f"events d0={d0} {per:3s} coin ~ {f}: n={len(Y)} alpha {coef[0]:+.4f} (t {coef[0]/se[0]:.2f}) beta {coef[1]:.2f} (t {coef[1]/se[1]:.2f}) mean factor {Y[f].mean():+.4f} -> beta part {coef[1]*Y[f].mean():+.4f} of mean coin {Y.coin.mean():+.4f}")
out['event_regressions'] = reg
json.dump(out, open('hedge.json', 'w'), indent=1)
