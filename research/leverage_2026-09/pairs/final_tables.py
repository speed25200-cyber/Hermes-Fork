"""Final tables -> out/results.json (numbers straight from the grid CSVs produced by pairs_v2.py / run_v2_low.py /
pairs_15m.py / run_gross.py)."""
import os, json
import numpy as np, pandas as pd
from analyze import load, paired, dist, CFG, OUT

df = load()
m1 = df[df.liq == 'm1']
h1 = df[df.liq == 'h1']
res = {}


def row(r, extra=None):
    d = {k: (v.item() if isinstance(v, (np.generic,)) else v) for k, v in r.items()}
    if extra:
        d.update(extra)
    return d


for name, d in [('m1', m1), ('h1', h1)]:
    j = paired(d)
    ok = j[(j.liqs_is == 0)]
    # (b) IS selection per leverage: max IS CAGR over every tunable (scheme, signal, exec, margin)
    per_lev = []
    for lev, g in j.groupby(level='lev'):
        b = g.sort_values(['cagr_is', 'sharpe_is'], ascending=False).iloc[0]
        per_lev.append(row(b, dict(zip(CFG + ['lev'], b.name))))
    # Sharpe selection among IS runs that were never liquidated
    per_lev_sh = []
    for lev, g in ok.groupby(level='lev'):
        b = g.sort_values(['sharpe_is', 'cagr_is'], ascending=False).iloc[0]
        per_lev_sh.append(row(b, dict(zip(CFG + ['lev'], b.name))))
    # (a) ladder of the config chosen on IS at 1x (cross margin)
    b1 = [r for r in per_lev if r['lev'] == 1][0]
    key = tuple(b1[c] for c in CFG)
    ladder = []
    for margin in ['cross', 'sleeve']:
        k2 = key[:-1] + (margin,)
        for lev in [1, 3, 5, 10, 15, 20]:
            try:
                r = j.loc[k2 + (lev,)]
                ladder.append(row(r, dict(zip(CFG + ['lev'], k2 + (lev,)))))
            except KeyError:
                pass
    # (c) overall IS-best (config, leverage)
    allb = j.sort_values(['cagr_is', 'sharpe_is'], ascending=False).iloc[0]
    res[name] = dict(distribution=dist(j).to_dict('records'), is_selected_per_lev=per_lev,
                     is_selected_per_lev_sharpe=per_lev_sh, ladder_of_1x_choice=ladder,
                     overall_is_best=row(allb, dict(zip(CFG + ['lev'], allb.name))))

# gross 1x diagnostic
g = pd.read_csv(os.path.join(OUT, 'grid_gross_1x.csv.gz'))
gi = g[g.period == 'IS']; go = g[g.period == 'OOS']
res['gross_1x'] = dict(n=len(gi), is_pos=int((gi.cagr > 0).sum()), oos_pos=int((go.cagr > 0).sum()),
                       is_med=float(gi.cagr.median()), oos_med=float(go.cagr.median()))
# 15m
p15 = os.path.join(OUT, 'grid_15m.csv.gz')
if os.path.exists(p15):
    q = pd.read_csv(p15)
    c15 = ['method', 'hedge', 'Wf', 'K', 'Wz', 'zin', 'zout', 'zstop', 'exec', 'lev']
    qi = q[q.period == 'IS'].set_index(c15); qo = q[q.period == 'OOS'].set_index(c15)
    cols = ['cagr', 'sharpe', 'maxdd_intrabar', 'worst_day', 'trades', 'liqs', 'per_year']
    qj = qi[cols].join(qo[cols], lsuffix='_is', rsuffix='_oos')
    d15 = []
    for (ex, lev), gg in qj.groupby([qj.index.get_level_values('exec'), qj.index.get_level_values('lev')]):
        d15.append(dict(exec=ex, lev=lev, n=len(gg), is_pos=int((gg.cagr_is > 0).sum()),
                        oos_pos=int((gg.cagr_oos > 0).sum()), is_med=gg.cagr_is.median(),
                        oos_med=gg.cagr_oos.median(), is_max=gg.cagr_is.max(), oos_max=gg.cagr_oos.max(),
                        liq_oos=int((gg.liqs_oos > 0).sum())))
    sel15 = []
    qn = qj[qj.index.get_level_values('exec') != 'gross']
    for lev, gg in qn.groupby(level='lev'):
        b = gg.sort_values(['cagr_is', 'sharpe_is'], ascending=False).iloc[0]
        sel15.append(row(b, dict(zip(c15, b.name))))
    res['m15'] = dict(distribution=d15, is_selected_per_lev=sel15)
json.dump(res, open(os.path.join(OUT, 'results.json'), 'w'), indent=1, default=str)

pd.set_option('display.width', 250)
for name in ['m1', 'h1']:
    print(f'===== {name} ladder of the 1x IS choice')
    L = pd.DataFrame(res[name]['ladder_of_1x_choice'])
    print(L[['margin', 'lev', 'cagr_is', 'cagr_oos', 'sharpe_is', 'sharpe_oos', 'maxdd_intrabar_is', 'maxdd_intrabar_oos',
             'worst_day_is', 'worst_day_oos', 'liqs_is', 'liqs_oos', 'trades_is', 'trades_oos', 'per_year_is',
             'per_year_oos']].round(3).to_string(index=False))
    print(f'===== {name} IS-selected per leverage')
    L = pd.DataFrame(res[name]['is_selected_per_lev'])
    print(L[CFG + ['lev', 'cagr_is', 'cagr_oos', 'sharpe_oos', 'maxdd_intrabar_oos', 'worst_day_oos', 'liqs_oos',
             'trades_oos', 'per_year_is', 'per_year_oos']].round(3).to_string(index=False))
    print(f'===== {name} IS-selected per leverage by IS Sharpe (no IS liquidation)')
    L = pd.DataFrame(res[name]['is_selected_per_lev_sharpe'])
    print(L[CFG + ['lev', 'cagr_is', 'sharpe_is', 'cagr_oos', 'sharpe_oos', 'maxdd_intrabar_oos', 'liqs_oos',
             'per_year_oos']].round(3).to_string(index=False))
    b = res[name]['overall_is_best']
    print(f'===== {name} overall IS best:', {k: b[k] for k in CFG + ['lev', 'cagr_is', 'cagr_oos', 'maxdd_intrabar_oos', 'liqs_oos', 'per_year_oos']})
print('gross', res['gross_1x'])
if 'm15' in res:
    print(pd.DataFrame(res['m15']['distribution']).round(3).to_string(index=False))
    print(pd.DataFrame(res['m15']['is_selected_per_lev'])[['method', 'hedge', 'Wf', 'K', 'Wz', 'zin', 'zout', 'zstop', 'exec', 'lev', 'cagr_is', 'cagr_oos', 'sharpe_oos', 'maxdd_intrabar_oos', 'liqs_oos', 'trades_oos', 'per_year_oos']].round(3).to_string(index=False))
