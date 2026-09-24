"""Compile out/results.json: grid distributions, IS-selected configs (S1 per L, S2), zero-cost and no-range
sensitivities, event study, stage-2 post-hoc dip-buy, validation notes."""
import json, os
import numpy as np, pandas as pd
OUT = 'out'
m = pd.read_csv(f'{OUT}/grid_wick_grid_orb.csv'); z = pd.read_csv(f'{OUT}/grid_zerocost_wick_grid_orb.csv')
nr = pd.read_csv(f'{OUT}/grid_norange_wick_grid_orb.csv'); s2 = pd.read_csv(f'{OUT}/stage2_dipbuy.csv')
dist = pd.read_csv(f'{OUT}/summary_distribution.csv'); sel = pd.read_csv(f'{OUT}/selected.csv'); ev = pd.read_csv(f'{OUT}/wick_event_study.csv')
def wide(df):
    p = df.pivot_table(index=['family', 'cfg_id', 'L'], columns='period', values=['cagr', 'maxdd', 'liq', 'sharpe', 'trades', 'worst_day']).reset_index()
    p.columns = ['_'.join([c for c in col if c]) for col in p.columns]
    return p
def dist_of(df):
    p = wide(df)
    return {f'{fam}_L{L}': dict(n=len(g), IS_pos=int((g.cagr_IS > 0).sum()), OOS_pos=int((g.cagr_OOS > 0).sum()),
                                 both_pos=int(((g.cagr_IS > 0) & (g.cagr_OOS > 0)).sum()), OOS_median=round(float(g.cagr_OOS.median()), 4),
                                 OOS_max=round(float(g.cagr_OOS.max()), 4))
            for (fam, L), g in p.groupby(['family', 'L'])}
full = m.set_index(['family', 'cfg_id', 'L', 'period'])
rows = []
for _, r in sel[(sel.rule == 'S1_bestIS_CAGR_at_L') & (sel.period == 'OOS')].iterrows():
    ris = full.loc[(r.family, r.cfg_id, r.L, 'IS')]
    rows.append(dict(variant=f'{r.family} S1 (best IS CAGR at this L) {r.cfg}; trades IS/OOS {int(ris.trades)}/{int(r.trades)}; liq IS/OOS {int(ris.liq)}/{int(r.liq)}',
                     family=r.family, leverage=int(r.L), cagr_is=round(float(ris.cagr), 4), cagr_oos=round(float(r.cagr), 4), maxdd=round(float(r.maxdd), 4),
                     maxdd_is=round(float(ris.maxdd), 4), worst_day=round(float(r.worst_day), 4), liquidations=int(r.liq), sharpe_oos=round(float(r.sharpe), 3),
                     per_year=json.dumps({**json.loads(ris.per_year), **json.loads(r.per_year)})))
# stage 2 (post-hoc): best IS CAGR per L among its 12 configs
s2w = s2.pivot_table(index=['cfg_id', 'L'], columns='period', values=['cagr', 'maxdd', 'liq', 'sharpe', 'trades', 'worst_day']).reset_index()
s2w.columns = ['_'.join([c for c in col if c]) for col in s2w.columns]
s2f = s2.set_index(['cfg_id', 'L', 'period'])
s2rows = []
for L, g in s2w.groupby('L'):
    b = g.sort_values('cagr_IS', ascending=False).iloc[0]
    ro = s2f.loc[(b.cfg_id, L, 'OOS')]; ri = s2f.loc[(b.cfg_id, L, 'IS')]
    s2rows.append(dict(variant=f'POST-HOC stage-2 dip-buy (best IS CAGR at this L) {ro.cfg}; trades IS/OOS {int(ri.trades)}/{int(ro.trades)}; liq IS/OOS {int(ri.liq)}/{int(ro.liq)}',
                       family='wick_stage2_posthoc', leverage=int(L), cagr_is=round(float(ri.cagr), 4), cagr_oos=round(float(ro.cagr), 4), maxdd=round(float(ro.maxdd), 4),
                       maxdd_is=round(float(ri.maxdd), 4), worst_day=round(float(ro.worst_day), 4), liquidations=int(ro.liq), sharpe_oos=round(float(ro.sharpe), 3),
                       per_year=json.dumps({**json.loads(ri.per_year), **json.loads(ro.per_year)})))
s2dist = {f'L{L}': dict(n=len(g), IS_pos=int((g.cagr_IS > 0).sum()), OOS_pos=int((g.cagr_OOS > 0).sum()), both=int(((g.cagr_IS > 0) & (g.cagr_OOS > 0)).sum()),
                        liq_any_OOS=int((g.liq_OOS > 0).sum())) for L, g in s2w.groupby('L')}
res = dict(
    distribution_main=dist.round(4).to_dict(orient='records'),
    selected_S1=rows, selected_all=sel.drop(columns=['coin_cagr']).round(4).to_dict(orient='records'),
    zero_cost_L1=dist_of(z), no_range_slippage=dist_of(nr),
    event_study=ev.to_dict(orient='records'),
    stage2_posthoc=dict(selected=s2rows, distribution=s2dist),
)
json.dump(res, open(f'{OUT}/results.json', 'w'), indent=1, default=float)
print(json.dumps(rows + s2rows, indent=0)[:200])
pd.DataFrame(rows + s2rows).to_csv(f'{OUT}/results_selected_summary.csv', index=False)
print(pd.DataFrame(rows + s2rows)[['family', 'leverage', 'cagr_is', 'cagr_oos', 'maxdd', 'worst_day', 'liquidations', 'sharpe_oos']].to_string())
print(json.dumps(s2dist))
