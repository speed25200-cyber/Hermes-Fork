"""Aggregate the grids: distribution per leverage, IS-selected configs and their OOS results -> out/summary.json."""
import os, json, sys
import numpy as np, pandas as pd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
CFG = ['method', 'hedge', 'Wf', 'K', 'Wz', 'zin', 'zout', 'zstop', 'exec', 'margin']


def load():
    v2 = pd.read_csv(os.path.join(OUT, 'grid_v2.csv.gz'))
    low = os.path.join(OUT, 'grid_v2_low.csv.gz')
    if os.path.exists(low):
        v2 = pd.concat([v2, pd.read_csv(low)], ignore_index=True)
    return v2


def paired(df):
    i = df[df.period == 'IS'].set_index(CFG + ['lev'])
    o = df[df.period == 'OOS'].set_index(CFG + ['lev'])
    cols = ['cagr', 'sharpe', 'maxdd_intrabar', 'worst_day', 'trades', 'liqs', 'per_year', 'exposure', 'fees',
            'funding', 'avg_gross_lev']
    return i[cols].join(o[cols], lsuffix='_is', rsuffix='_oos')


def dist(j):
    rows = []
    for (ex, mg, lev), g in j.groupby([j.index.get_level_values('exec'), j.index.get_level_values('margin'),
                                       j.index.get_level_values('lev')]):
        rows.append(dict(exec=ex, margin=mg, lev=lev, n=len(g), is_pos=int((g.cagr_is > 0).sum()),
                         oos_pos=int((g.cagr_oos > 0).sum()), both_pos=int(((g.cagr_is > 0) & (g.cagr_oos > 0)).sum()),
                         is_med=g.cagr_is.median(), oos_med=g.cagr_oos.median(), is_max=g.cagr_is.max(),
                         oos_max=g.cagr_oos.max(), liq_is=int((g.liqs_is > 0).sum()),
                         liq_oos=int((g.liqs_oos > 0).sum()), oos_ruined=int((g.cagr_oos <= -0.999).sum())))
    return pd.DataFrame(rows)


def pick(j, by='cagr_is'):
    """IS selection per leverage (all other tunables free: pair scheme, signal, exec, margin)."""
    rows = []
    for lev, g in j.groupby(j.index.get_level_values('lev')):
        g = g.sort_values([by, 'sharpe_is'], ascending=False)
        b = g.iloc[0]
        r = dict(zip(CFG + ['lev'], b.name))
        r.update({k: (v if not isinstance(v, (np.floating, np.integer)) else v.item()) for k, v in b.items()})
        rows.append(r)
    return pd.DataFrame(rows)


if __name__ == '__main__':
    df = load()
    summary = {}
    for liq in ['h1', 'm1']:
        d = df[df.liq == liq]
        if liq == 'm1':
            # lev 1/3 cross come from the supplementary run
            pass
        j = paired(d)
        ds = dist(j)
        print(f'==== liquidation bound {liq}: distribution over the grid (configs = 28 pair schemes x 72 signal settings)')
        print(ds.round(3).to_string(index=False))
        p = pick(j)
        print(f'==== {liq}: IS-selected config per leverage (max IS CAGR)')
        print(p[CFG + ['lev', 'cagr_is', 'cagr_oos', 'sharpe_is', 'sharpe_oos', 'maxdd_intrabar_is', 'maxdd_intrabar_oos',
                       'worst_day_oos', 'liqs_is', 'liqs_oos', 'trades_is', 'trades_oos', 'per_year_oos']].round(3).to_string(index=False))
        ps = pick(j, 'sharpe_is')
        print(f'==== {liq}: IS-selected by IS Sharpe')
        print(ps[CFG + ['lev', 'cagr_is', 'cagr_oos', 'sharpe_is', 'sharpe_oos', 'maxdd_intrabar_oos', 'liqs_oos']].round(3).to_string(index=False))
        summary[liq] = dict(distribution=ds.to_dict('records'), is_best_cagr=p.to_dict('records'),
                            is_best_sharpe=ps.to_dict('records'))
    json.dump(summary, open(os.path.join(OUT, 'summary.json'), 'w'), indent=1, default=str)
