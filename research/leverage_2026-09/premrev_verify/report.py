"""IS-only selection + OOS report + whole-grid distribution from out/grid_results_<tag>.csv.
For each (exec scenario, margin model, leverage): pick the config with the best IS CAGR among configs that
could actually be opened at that leverage (>=1 IS trade not rejected), report its OOS numbers.
Writes out/selection_<tag>.csv, out/distribution_<tag>.csv, out/results_<tag>.json."""
import sys, json
import numpy as np, pandas as pd

OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev_verify/out'
tag = sys.argv[1] if len(sys.argv) > 1 else 'main'
df = pd.read_csv(f'{OUT}/grid_results_{tag}.csv.gz')
KEY = ['exec', 'lat', 'univ', 'W', 'k', 'x', 'H', 'both', 'confirm']
IS = df[df.seg == 'IS'].set_index(KEY + ['margin', 'L'])
OOS = df[df.seg == 'OOS'].set_index(KEY + ['margin', 'L'])
sel_rows, dist_rows = [], []
for (ex, lat), g in df[df.seg == 'IS'].groupby(['exec', 'lat']):
    for m in ['mc', 'pm', 'sep']:
        for L in sorted(df.L.unique()):
            gi = g[(g.margin == m) & (g.L == L)]
            feas = gi[gi.trades > 0]
            o = OOS.loc[[tuple(r[KEY]) + (m, L) for _, r in gi.iterrows()]]
            of = o[o.trades > 0]
            dist_rows.append(dict(exec=ex, lat=lat, margin=m, L=L, n_cfg=len(gi), n_feasible_IS=len(feas),
                                  n_IS_pos=int((feas.cagr > 0).sum()), n_OOS_feasible=len(of),
                                  n_OOS_pos=int((of.cagr > 0).sum()),
                                  n_both_pos=int(((gi.cagr.values > 0) & (o.cagr.values > 0) & (gi.trades.values > 0)).sum()),
                                  OOS_cagr_median=float(of.cagr.median()) if len(of) else np.nan,
                                  OOS_cagr_q90=float(of.cagr.quantile(.9)) if len(of) else np.nan,
                                  OOS_cagr_max=float(of.cagr.max()) if len(of) else np.nan,
                                  n_OOS_liq=int((of.liqs > 0).sum()),
                                  IS_cagr_median=float(feas.cagr.median()) if len(feas) else np.nan))
            if not len(feas):
                sel_rows.append(dict(exec=ex, lat=lat, margin=m, L=L, feasible=False))
                continue
            best = feas.sort_values(['cagr', 'sharpe'], ascending=False).iloc[0]
            ob = OOS.loc[tuple(best[KEY]) + (m, L)]
            sel_rows.append(dict(exec=ex, lat=lat, margin=m, L=L, feasible=True,
                                 cfg=best['univ'] + ' W%d k%d x%.1f H%d both%d conf%d' % tuple(best[['W', 'k', 'x', 'H', 'both', 'confirm']]),
                                 cagr_is=best.cagr, maxdd_is=best.maxdd, liqs_is=best.liqs, trades_is=best.trades,
                                 sharpe_is=best.sharpe, years_is=best.years, edge_bp_is=best.edge_bp, edge_t_is=best.edge_t,
                                 cagr_oos=ob.cagr, maxdd_oos=ob.maxdd, worst_day_oos=ob.worst_day, liqs_oos=ob.liqs,
                                 trades_oos=ob.trades, rejected_oos=ob.rejected, sharpe_oos=ob.sharpe, years_oos=ob.years,
                                 edge_bp_oos=ob.edge_bp, edge_t_oos=ob.edge_t, n_oos_all=ob.n_all))
sel = pd.DataFrame(sel_rows)
dist = pd.DataFrame(dist_rows)
sel.to_csv(f'{OUT}/selection_{tag}.csv', index=False, float_format='%.5g')
dist.to_csv(f'{OUT}/distribution_{tag}.csv', index=False, float_format='%.5g')
# 1x unlevered per-trade edge distribution per exec (independent of margin/leverage)
e1 = df[(df.margin == 'pm') & (df.L == 1)]
edge = e1.pivot_table(index=KEY, columns='seg', values=['edge_bp', 'n_all', 'cagr']).reset_index()
edge.columns = ['_'.join([c for c in col if c]) for col in edge.columns]
edist = edge.groupby(['exec', 'lat']).apply(lambda g: pd.Series(dict(
    n=len(g), IS_edge_pos=int((g.edge_bp_IS > 0).sum()), OOS_edge_pos=int((g.edge_bp_OOS > 0).sum()),
    both_pos=int(((g.edge_bp_IS > 0) & (g.edge_bp_OOS > 0)).sum()),
    IS_edge_median=g.edge_bp_IS.median(), OOS_edge_median=g.edge_bp_OOS.median(),
    IS_edge_max=g.edge_bp_IS.max(), OOS_edge_max=g.edge_bp_OOS.max(),
    corr_IS_OOS=g[['edge_bp_IS', 'edge_bp_OOS']].corr().iloc[0, 1]))).reset_index()
edist.to_csv(f'{OUT}/edge_distribution_{tag}.csv', index=False, float_format='%.4g')
json.dump(dict(selection=sel.replace({np.nan: None}).to_dict('records'), distribution=dist.to_dict('records'),
               edge_distribution=edist.to_dict('records')), open(f'{OUT}/results_{tag}.json', 'w'), indent=1, default=float)
pd.set_option('display.width', 260); pd.set_option('display.max_rows', 500); pd.set_option('display.max_columns', 40)
print(edist.round(2).to_string())
cols = ['exec', 'lat', 'margin', 'L', 'cfg', 'cagr_is', 'maxdd_is', 'liqs_is', 'trades_is', 'edge_bp_is', 'cagr_oos', 'maxdd_oos',
        'worst_day_oos', 'liqs_oos', 'trades_oos', 'sharpe_oos', 'edge_bp_oos', 'years_oos']
print(sel[[c for c in cols if c in sel]].round(3).to_string())
print(dist.round(3).to_string())
