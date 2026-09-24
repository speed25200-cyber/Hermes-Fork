"""Distribution + IS selection under the stricter maker fill rules (v6 grid), with taker rows from the reported grid."""
import pandas as pd, numpy as np, json
C = ['method', 'hedge', 'Wf', 'K', 'Wz', 'zin', 'zout', 'zstop']
v = pd.read_csv('out/v6_grid_tick.csv.gz')
g = pd.concat([pd.read_csv('../pairs/out/grid_v2.csv.gz'), pd.read_csv('../pairs/out/grid_v2_low.csv.gz')])
g = g[(g.liq == 'm1') & (g.margin == 'cross')].rename(columns={'maxdd_intrabar': 'maxdd'})
g['tick_bp'] = np.where(g.exec == 'maker', 1.0, np.nan)
v['exec'] = 'maker'
allr = pd.concat([g[C + ['exec', 'tick_bp', 'lev', 'period', 'cagr', 'sharpe', 'maxdd', 'worst_day', 'liqs', 'trades', 'per_year']],
                  v[C + ['exec', 'tick_bp', 'lev', 'period', 'cagr', 'sharpe', 'maxdd', 'worst_day', 'liqs', 'trades', 'per_year']]])
allr['rule'] = np.where(allr.exec == 'taker', 'taker', 'maker_' + allr.tick_bp.fillna(0).astype(int).astype(str) + 'bp')
K = C + ['rule', 'lev']
j = allr[allr.period == 'IS'].set_index(K).join(allr[allr.period == 'OOS'].set_index(K), lsuffix='_is', rsuffix='_oos')
pd.set_option('display.width', 250)
d = j.groupby(['rule', 'lev']).agg(n=('cagr_oos', 'size'), oos_pos=('cagr_oos', lambda x: int((x > 0).sum())),
                                   both_pos=('cagr_oos', lambda x: int(((x > 0) & (j.loc[x.index, 'cagr_is'] > 0)).sum())),
                                   med_is=('cagr_is', 'median'), med_oos=('cagr_oos', 'median'),
                                   ruined_oos=('cagr_oos', lambda x: int((x <= -0.999).sum())))
print(d.round(4).to_string())
out = {'distribution': d.reset_index().to_dict('records')}
for pool_name, rules in [('maker5bp+taker', ['maker_5bp', 'taker']), ('maker10bp+taker', ['maker_10bp', 'taker']),
                         ('maker1bp+taker (reported pool, cross only)', ['maker_1bp', 'taker'])]:
    q = j[j.index.get_level_values('rule').isin(rules)]
    rows = []
    for lev, gg in q.groupby(level='lev'):
        b = gg.sort_values(['cagr_is', 'sharpe_is'], ascending=False).iloc[0]
        rows.append(dict(zip(K, b.name)) | dict(cagr_is=b.cagr_is, cagr_oos=b.cagr_oos, maxdd_oos=b.maxdd_oos,
                                                worst_day_oos=b.worst_day_oos, liqs_oos=b.liqs_oos, trades_oos=b.trades_oos,
                                                sharpe_oos=b.sharpe_oos, per_year_is=b.per_year_is, per_year_oos=b.per_year_oos))
    r = pd.DataFrame(rows)
    print('=== IS-selected per leverage, pool:', pool_name)
    print(r.drop(columns=['per_year_is']).round(4).to_string(index=False))
    out[pool_name] = rows
json.dump(out, open('out/v9_summary_tick.json', 'w'), indent=1, default=str)
