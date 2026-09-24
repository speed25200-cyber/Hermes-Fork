"""Verifier: distribution summary (same statistics as analyze.py) for any grid CSV, plus S1 picks' OOS CAGR per L."""
import sys, json
import numpy as np, pandas as pd
f = sys.argv[1]
df = pd.read_csv(f)
piv = df.pivot_table(index=['family', 'cfg_id', 'L'], columns='period', values=['cagr', 'maxdd', 'liq']); piv.columns = [f'{a}_{b}' for a, b in piv.columns]; piv = piv.reset_index()
rows = []
for (fam, L), g in piv.groupby(['family', 'L']):
    b = g.sort_values('cagr_IS', ascending=False).iloc[0]
    rows.append(dict(family=fam, L=L, n=len(g), IS_pos=int((g.cagr_IS > 0).sum()), OOS_pos=int((g.cagr_OOS > 0).sum()),
                     both_pos=int(((g.cagr_IS > 0) & (g.cagr_OOS > 0)).sum()), OOS_med=g.cagr_OOS.median(), OOS_max=g.cagr_OOS.max(),
                     liq_any_OOS=int((g.liq_OOS > 0).sum()), S1_cfg=int(b.cfg_id), S1_IS=b.cagr_IS, S1_OOS=b.cagr_OOS, S1_ddOOS=b.maxdd_OOS))
out = pd.DataFrame(rows)
pd.set_option('display.width', 250)
print(out.round(3).to_string())
tot = out.groupby('L')[['OOS_pos', 'both_pos']].sum()
print('all families, configs profitable OOS / both, per L:'); print(tot.T.to_string())
