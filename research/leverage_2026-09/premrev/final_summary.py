"""Collect the numbers produced by run_grid/report/tick_check/okx_tick/okx_scan/levtable into one JSON."""
import json, numpy as np, pandas as pd

OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev/out'
res = {}
sel = pd.read_csv(f'{OUT}/selection_main.csv')
res['binance_proxy_is_selected'] = sel[sel.feasible].replace({np.nan: None}).to_dict('records')
res['binance_proxy_infeasible'] = sel[~sel.feasible][['exec', 'lat', 'margin', 'L']].to_dict('records')
res['grid_distribution'] = pd.read_csv(f'{OUT}/distribution_main.csv').replace({np.nan: None}).to_dict('records')
res['edge_distribution_1x'] = pd.read_csv(f'{OUT}/edge_distribution_main.csv').to_dict('records')
for t in ['kappa002', 'kappa010', 'stress1']:
    s = pd.read_csv(f'{OUT}/selection_{t}.csv')
    res[f'sensitivity_{t}'] = s[s.feasible & (s.margin == 'pm')].replace({np.nan: None}).to_dict('records')
bt = pd.read_csv(f'{OUT}/tick_check_sel.csv', parse_dates=['t'])
bt['seg'] = np.where(bt.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
cols = ['model_net_bp'] + [c for c in bt if c.startswith('net_')]
res['binance_tick_recheck_mean_net_bp'] = {s: g[cols].mean().round(2).to_dict() for s, g in bt.groupby('seg')}
ok = pd.read_csv(f'{OUT}/okx_tick_check_sel.csv', parse_dates=['t'])
ok['seg'] = np.where(ok.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
cols = ['bn_model_net_bp'] + [c for c in ok if c.startswith('okx_net_')]
res['okx_same_timestamps_mean_net_bp'] = {s: g[cols].mean().round(2).to_dict() for s, g in ok.groupby('seg')}
sc = pd.read_csv(f'{OUT}/okx_scan_trades.csv', parse_dates=['t'])
sc['seg'] = np.where(sc.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
g = sc.groupby(['k_bp', 'lat', 'seg']).agg(n=('net_bp', 'size'), mean_net_bp=('net_bp', 'mean'), sum_net_bp=('net_bp', 'sum'),
                                            worst_adverse_bp=('worst_bp', 'min')).reset_index()
res['okx_native_scan'] = g.round(2).to_dict('records')
info = pd.read_csv(f'{OUT}/okx_scan_days_info.csv')
info['maxdev'] = info.msg.str.extract(r'=(\d+)bp').astype(float)
res['okx_native_scan_days'] = dict(n_coin_days=len(info), maxdev_bp_quantiles=info.maxdev.quantile([.5, .9, .95, 1]).round(0).to_dict())
res['leverage_tables'] = pd.read_csv(f'{OUT}/levtables.csv').replace({np.nan: None}).to_dict('records')
json.dump(res, open(f'{OUT}/results_final.json', 'w'), indent=1, default=float)
print('written', f'{OUT}/results_final.json')
