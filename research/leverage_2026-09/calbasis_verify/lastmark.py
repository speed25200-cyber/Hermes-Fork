import pandas as pd, numpy as np
from load import load
import cb_v as cb
AS = load()
B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
pd.set_option('display.width', 250)
rows = []
for per, (a, b) in {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55')}.items():
    for asset in ['BTC', 'ETH']:
        A = AS[asset]
        s = cb.Sim(A, B, 1, 'cross', start=a, end=b).run()
        for t in s.trades:
            con = [c for c in A['cons'] if c['name'] == t['con']][0]
            for side, ts in (('in', t['entry']), ('out', t['exit'])):
                x = cb.bar_of(ts); k = x - con['b0']
                if side == 'out' and t['why'] == 'expiry':
                    continue
                F, Fm, P, Pm = con['F'][k], con['Fmc'][k], A['P'][x], A['Pmc'][x]
                # 1h window around fill: mean of last-premium and mark-premium
                kk = slice(max(k - 11, 0), k + 1); xx = slice(x - 11 - (k - max(k - 11, 0)) + 11 - 11, x + 1)
                rows.append(dict(per=per, asset=asset, con=t['con'], side=side, ts=ts, why=t['why'],
                                 lastprem_bp=(F / P - 1) * 1e4, markprem_bp=(Fm / Pm - 1) * 1e4,
                                 F_last_vs_mark_bp=(F / Fm - 1) * 1e4, P_last_vs_mark_bp=(P / Pm - 1) * 1e4, qv=con['qv'][k]))
d = pd.DataFrame(rows)
d['basis_gap_bp'] = d.lastprem_bp - d.markprem_bp  # >0 on entry helps a short-future entry; <0 on exit helps
print(d.round(1).to_string())
print(d.groupby(['per', 'side']).basis_gap_bp.agg(['mean', 'median', 'count']).round(1))
# unconditional distribution of last-vs-mark premium gap over all hours when contracts have tau>=30d
gaps = []
for asset in ['BTC', 'ETH']:
    A = AS[asset]
    for con in A['cons']:
        n = len(con['F']); idx = np.arange(con['b0'], con['b0'] + n)
        g = (con['F'] / A['P'][idx] - con['Fmc'] / A['Pmc'][idx]) * 1e4
        ok = (con['tau'] >= 30) & (cb.GRID[idx].minute == 0)
        gaps.append(pd.DataFrame(dict(asset=asset, year=cb.GRID[idx][ok].year, gap=g[ok])))
G = pd.concat(gaps)
print(G.groupby(['asset', 'year']).gap.describe(percentiles=[.05, .5, .95]).round(1))
d.to_csv('fill_last_vs_mark.csv', index=False)
