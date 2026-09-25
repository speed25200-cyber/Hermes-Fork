"""Collects every result file into results/summary.json (numbers only from the scripts' outputs)."""
import json, os
import numpy as np, pandas as pd
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')
S = {}
for price in ('hybrid', 'binance'):
    g = pd.read_csv(os.path.join(R, f'grid_{price}.csv'))
    el = g[(g.is_trades >= 30) & (~g.is_liq)]
    d = dict(n_configs=len(g), n_eligible=len(el), is_liquidated=int(g.is_liq.sum()), oos_liquidated=int(g.oos_liq.sum()),
             spearman_is_oos=float(g[['is_sharpe', 'oos_sharpe']].corr('spearman').iloc[0, 1]))
    for side, nm in ((-1, 'short'), (1, 'long')):
        x = g[g.side == side]
        d[nm] = dict(n=len(x), is_q10_50_90=x.is_sharpe.quantile([.1, .5, .9]).round(3).tolist(),
                     oos_q10_50_90=x.oos_sharpe.quantile([.1, .5, .9]).round(3).tolist(),
                     frac_oos_ge_1_5=float((x.oos_sharpe >= 1.5).mean()),
                     frac_oos_ge_1_5_and_both_years=float(((x.oos_sharpe >= 1.5) & (x.y2025 > 0) & (x.y2026 > 0)).mean()))
    top = el.sort_values('is_sharpe', ascending=False).head(10)
    d['top10_is'] = top[['side', 'd0', 'd1', 'beta', 'stop', 'uni', 'is_sharpe', 'is_trades', 'oos_sharpe', 'oos_cagr', 'oos_maxdd',
                         'oos_trades', 'y2025', 'y2026']].round(4).to_dict('records')
    ev = json.load(open(os.path.join(R, f'evaluation_{price}.json')))
    d['selected'] = {k: v for k, v in ev[0].items() if k != 'lomo'}
    d['next_is_ranked'] = [{k: e[k] for k in ('label', 'cfg', 'bar1', 'bar2', 'lomo_min', 'loco_min', 'cost15_lat1', 'supportable_L')} |
                           {'oos_sharpe': e['oos']['sharpe'], 'years': e['oos']['years']} for e in ev[1:]]
    S[price] = d
for f in ('executability', 'leverage_total', 'agefactor_eval', 'book_corr'):
    S[f] = json.load(open(os.path.join(R, f + '.json')))
S['althedge'] = pd.read_csv(os.path.join(R, 'althedge.csv')).round(4).to_dict('records')
fh = pd.read_csv(os.path.join(R, 'firsthours.csv'))
isb = fh[fh.per == 'IS'].sort_values('port_sharpe', ascending=False).iloc[0]
oo = fh[(fh.per == 'OOS') & (fh.side == isb.side) & (fh.h0 == isb.h0) & (fh.h1 == isb.h1)].iloc[0]
S['firsthours'] = dict(n_rules=int(len(fh) // 2), is_best=dict(side=int(isb.side), h0=int(isb.h0), h1=int(isb.h1), is_port_sharpe=float(isb.port_sharpe),
                       is_mean=float(isb['mean']), oos_port_sharpe=float(oo.port_sharpe), oos_mean=float(oo['mean']), n_is=int(isb.n), n_oos=int(oo.n)),
                       oos_port_sharpe_q10_50_90=fh[fh.per == 'OOS'].port_sharpe.quantile([.1, .5, .9]).round(3).tolist(),
                       short_mae_p90_72h=float(fh[(fh.side == -1) & (fh.h0 == 1) & (fh.h1 == 72)].mae_p90.max()))
json.dump(S, open(os.path.join(R, 'summary.json'), 'w'), indent=1, default=str)
print(json.dumps({k: S[k] for k in ('firsthours',)}, indent=1, default=str))
for p in ('hybrid', 'binance'):
    print(p, {k: v for k, v in S[p].items() if k in ('n_configs', 'n_eligible', 'is_liquidated', 'oos_liquidated', 'spearman_is_oos', 'short', 'long')})
