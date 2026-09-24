"""Verifier summary: for each execution-rule variant, per family and L: configs profitable OOS, profitable in both
windows, OOS median CAGR, configs with >=1 OOS liquidation, and the S1 (best IS CAGR) pick's IS/OOS CAGR.
Writes out/verify_summary.json and out/verify_summary.csv."""
import json
import numpy as np, pandas as pd
VAR = {'original (repro, opt0 + range slip)': 'grid_opt0_nors0_wick_grid_orb.csv',
       'stop-before-liq only (opt2)': 'grid_opt2_nors0_wick_grid_orb.csv',
       'maker touch fills only (opt1)': 'grid_opt1_nors0_wick_grid_orb.csv',
       'no range slip only (nors)': 'grid_opt0_nors1_wick_grid_orb.csv',
       'less-pessimistic ordering, same costs (opt14)': 'grid_opt14_nors0_wick_grid_orb.csv',
       'optimistic bound (opt15 + no range slip)': 'grid_opt15_nors1_wick_grid_orb.csv'}
rows = []
for name, f in VAR.items():
    df = pd.read_csv('out/' + f)
    p = df.pivot_table(index=['family', 'cfg_id', 'L'], columns='period', values=['cagr', 'maxdd', 'liq']); p.columns = [f'{a}_{b}' for a, b in p.columns]; p = p.reset_index()
    for (fam, L), g in p.groupby(['family', 'L']):
        b = g.sort_values('cagr_IS', ascending=False).iloc[0]
        rows.append(dict(variant=name, family=fam, L=int(L), n=len(g), OOS_pos=int((g.cagr_OOS > 0).sum()), both_pos=int(((g.cagr_IS > 0) & (g.cagr_OOS > 0)).sum()),
                         OOS_median=round(float(g.cagr_OOS.median()), 4), liq_any_OOS=int((g.liq_OOS > 0).sum()),
                         S1_cfg=int(b.cfg_id), S1_IS=round(float(b.cagr_IS), 4), S1_OOS=round(float(b.cagr_OOS), 4), S1_maxdd_OOS=round(float(b.maxdd_OOS), 4)))
out = pd.DataFrame(rows)
out.to_csv('out/verify_summary.csv', index=False)
tot = out.groupby(['variant', 'L'])[['OOS_pos', 'both_pos']].sum().reset_index()
json.dump(dict(per_family=rows, totals=tot.to_dict('records')), open('out/verify_summary.json', 'w'), indent=1)
pd.set_option('display.width', 250)
print(tot.pivot(index='variant', columns='L', values='OOS_pos').to_string())
print(tot.pivot(index='variant', columns='L', values='both_pos').to_string())
print(out[out.L >= 10][['variant', 'family', 'L', 'OOS_pos', 'both_pos', 'OOS_median', 'liq_any_OOS', 'S1_IS', 'S1_OOS']].to_string())
