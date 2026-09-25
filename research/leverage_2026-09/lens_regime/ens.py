"""(a) Ensembles that could have been fixed in advance, vs the single IS-picked config. Equal capital, daily
rebalanced (mean of the member configs' daily returns). IS = 2022-01..2024-12, OOS = 2025-01..2026-08.
-> ens.json, ens_daily.parquet"""
import json, numpy as np, pandas as pd
R = pd.read_parquet('daily_all.parquet')
liq = pd.read_csv('liq_all.csv').set_index('key')
g = pd.read_csv('../newlisting/results/grid_hybrid.csv')
def key(r, h='btc'):
    st = r.stop if r.stop != 0 else 0
    st = {0.0: 0, 0.25: 0.25, 0.5: 0.5, 1.0: 1.0}[float(st)]
    return f"s{int(r.side)}_d{int(r.d0)}_e{int(r.d1)}_b{int(r.beta)}_st{st}_{r.uni}_{h}"
g['key'] = [key(r) for r in g.itertuples()]
assert g.key.isin(R.columns).all(), g.key[~g.key.isin(R.columns)].head()
IS = R.loc[:'2024-12-31']; OOS = R.loc['2025-01-01':]
def sh(x):
    x = np.asarray(x, float); s = x.std(ddof=1); return float(x.mean() / s * np.sqrt(365)) if s > 0 else 0.0
# sanity: reproduce grid sharpes
chk = np.array([sh(IS[k]) for k in g.key]); print('max |IS sharpe diff vs grid|', np.abs(chk - g.is_sharpe).max())
chk = np.array([sh(OOS[k]) for k in g.key]); print('max |OOS sharpe diff vs grid|', np.abs(chk - g.oos_sharpe).max())
el = g[(g.is_trades >= 30) & (~g.is_liq)].sort_values('is_sharpe', ascending=False)
def yr(r):
    return {str(k): float(np.prod(1 + v) - 1) for k, v in r.groupby(r.index.year)}
def lomo(r):
    m = r.index.to_period('M'); v = {str(p): sh(r[m != p]) for p in m.unique()}
    k = min(v, key=v.get); return v[k], k
def stats(cols, name):
    r = R[cols].mean(axis=1)
    i, o = r.loc[:'2024-12-31'], r.loc['2025-01-01':]
    lm, lmk = lomo(o)
    mem_o = np.array([sh(OOS[c]) for c in cols]); mem_i = np.array([sh(IS[c]) for c in cols])
    def mdd(x):
        e = (1 + x).cumprod(); return float((1 - e / e.cummax()).max())
    return dict(name=name, n=len(cols), is_sharpe=sh(i), oos_sharpe=sh(o), years=yr(r), oos_lomo_min=lm, oos_lomo_month=lmk,
                oos_daily_maxdd=mdd(o), oos_vol=float(o.std() * np.sqrt(365)), is_vol=float(i.std() * np.sqrt(365)),
                members_is_median=float(np.median(mem_i)), members_oos_median=float(np.median(mem_o)),
                members_oos_min=float(mem_o.min()), members_oos_max=float(mem_o.max()),
                members_oos_frac_ge_1_5=float((mem_o >= 1.5).mean())), r
out, series = [], {}
sets = {}
sets['selected (IS rank 1)'] = el.key.head(1).tolist()
sets['IS top-5'] = el.key.head(5).tolist()
sets['IS top-10'] = el.key.head(10).tolist()
sets['IS top-20'] = el.key.head(20).tolist()
sets['IS top-50'] = el.key.head(50).tolist()
pl = g[(g.side == -1) & g.d0.isin([24, 72]) & (g.d1 == 7)]
sets['plateau d0 in {1d,3d} -> d7, all beta/stop/uni (32)'] = pl.key.tolist()
sets['plateau, BTC-hedged only (16)'] = pl[pl.beta == 1].key.tolist()
sets['plateau, BTC-hedged, newtok only (8)'] = pl[(pl.beta == 1) & (pl.uni == 'newtok')].key.tolist()
pl2 = g[(g.side == -1) & g.d0.isin([6, 24, 72]) & g.d1.isin([7, 14])]
sets['wider: d0 in {6h,1d,3d} x d1 in {7,14}, all (96)'] = pl2.key.tolist()
sets['all short configs (IS-eligible)'] = el[el.side == -1].key.tolist()
sets['all short configs, d1<=14d'] = el[(el.side == -1) & (el.d1 <= 14)].key.tolist()
for nm, cols in sets.items():
    s, r = stats(cols, nm); out.append(s); series[nm] = r
    print(f"{nm:55s} n={s['n']:3d} IS {s['is_sharpe']:.2f} OOS {s['oos_sharpe']:.2f} lomo {s['oos_lomo_min']:.2f}({s['oos_lomo_month']}) "
          f"yrs {' '.join(f'{k}:{v:+.3f}' for k, v in s['years'].items())} memOOS med {s['members_oos_median']:.2f} [{s['members_oos_min']:.2f},{s['members_oos_max']:.2f}] vol {s['oos_vol']:.3f}")
# grid-level: rank correlation IS vs OOS among short configs; OOS sharpe by IS decile
sg = el[el.side == -1].copy()
from scipy.stats import spearmanr
rho = spearmanr(sg.is_sharpe, sg.oos_sharpe)
sg['dec'] = pd.qcut(sg.is_sharpe.rank(method='first'), 10, labels=False)
dec = sg.groupby('dec').agg(is_med=('is_sharpe', 'median'), oos_med=('oos_sharpe', 'median'), n=('key', 'size'))
print('short configs n', len(sg), 'spearman IS vs OOS', rho)
print(dec)
lg = el[el.side == 1]
print('long configs n', len(lg), 'OOS median', lg.oos_sharpe.median(), 'IS median', lg.is_sharpe.median())
res = dict(ensembles=out, short_is_oos_spearman=[float(rho.correlation), float(rho.pvalue)], short_n=len(sg),
           oos_by_is_decile=dec.reset_index().to_dict('records'), short_oos_median=float(sg.oos_sharpe.median()),
           short_is_median=float(sg.is_sharpe.median()), long_oos_median=float(lg.oos_sharpe.median()), sets={k: v for k, v in sets.items()})
json.dump(res, open('ens.json', 'w'), indent=1)
pd.DataFrame(series).to_parquet('ens_daily.parquet')
