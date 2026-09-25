"""Collect the headline table (results_summary.csv) from results.json and the grid CSVs."""
import json
import numpy as np, pandas as pd
W = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/tsmom_div'
r = json.load(open(f'{W}/results.json'))
G = pd.read_csv(f'{W}/grid_tsmom.csv')
rows = []
t = r['tsmom']
lev1 = {x['L']: x for x in t['leverage']}
rows.append(dict(variant=f"TSMOM {t['config']} (IS-selected)", sharpe_is=t['is']['sharpe'], sharpe_oos=t['oos']['sharpe'],
                 cagr_oos=t['oos']['cagr'], maxdd_oos_intrabar_1x=lev1[1]['maxdd_intrabar'], y2025=t['years']['2025'],
                 y2026=t['years']['2026'], trades_oos=t['trades_oos'], half_kelly_is=t['kelly']['half_kelly'],
                 supportable_L=t['supportable_L'], lomo_min=t['robustness']['lomo_min'],
                 loco_min=t['robustness']['loco_min'], stress=t['robustness']['stress_cost15_lag1']))
for k, v in r['combos'].items():
    lv = {x['L']: x for x in v['leverage']}
    s = v['supportable_L']
    rows.append(dict(variant=f'combo {k} w={v["weights"]}', sharpe_is=v['sharpe_cis'], sharpe_oos=v['sharpe_oos'],
                     cagr_oos=v['cagr_oos'], maxdd_oos_intrabar_1x=lv[1]['maxdd_intrabar'], y2025=v['y2025'],
                     y2026=v['y2026'], half_kelly_is=v['kelly']['half_kelly'], supportable_L=s,
                     cagr_oos_at_supportable=lv[s]['cagr'] if s else np.nan,
                     maxdd_at_supportable=lv[s]['maxdd_intrabar'] if s else np.nan,
                     gross_avg_at_supportable=lv[s]['gross_avg'] if s else np.nan,
                     lomo_min=v['lomo_min'], loco_min=v['loco_min'], stress=v['stress_cost15_lag1']))
S = pd.DataFrame(rows)
S.to_csv(f'{W}/results_summary.csv', index=False)
pd.set_option('display.width', 250)
print(S.round(3).to_string())
print('TSMOM grid: n', len(G), 'IS', G.sharpe_is.describe().round(3).to_dict())
print('OOS', G.sharpe_oos.describe().round(3).to_dict())
print('OOS>=1.5:', int((G.sharpe_oos >= 1.5).sum()), ' OOS>=1.0:', int((G.sharpe_oos >= 1.0).sum()),
      ' both OOS years>0:', int(((G.sum_2025 > 0) & (G.sum_2026 > 0)).sum()),
      ' IS>=1.5:', int((G.sharpe_is >= 1.5).sum()), ' spearman IS/OOS', round(G[['sharpe_is', 'sharpe_oos']].corr('spearman').iloc[0, 1], 3))
print(G.groupby('lbs')[['sharpe_is', 'sharpe_oos']].mean().round(3))
print(G.groupby('N')[['sharpe_is', 'sharpe_oos']].mean().round(3))
for k in ['inv_vol_book_tsmom', 'equal_capital_3', 'book_alone', 'inv_vol_3']:
    print(k, [(x['L'], round(x['maxdd_intrabar'], 3), x['liquidated'], round(x['cagr'], 3), round(x['gross_avg'], 2), x['openable']) for x in r['combos'][k]['leverage']])
