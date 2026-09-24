import pandas as pd, numpy as np
from load import load, cb
AS = load()
p = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
pd.set_option('display.width', 250)
for per, (a, b) in {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}.items():
    for asset in ['BTC', 'ETH']:
        s = cb.Sim(AS[asset], p, 1, 'cross', start=a, end=b).run()
        tr = pd.DataFrame(s.trades); N = tr.notional
        tr['spr%'] = (tr.fut_pnl + tr.perp_pnl) / N * 100; tr['fund%'] = tr.fund / N * 100; tr['net%'] = (tr.Eend / tr.E0 - 1) * 100
        tr['prem_in%'] = tr.prem_in * 100
        # qv at entry bar and exit bar of the future
        A = AS[asset]
        qe, qx, stale_e, stale_x = [], [], [], []
        for _, r in tr.iterrows():
            con = [c for c in A['cons'] if c['name'] == r.con][0]
            xe = cb.bar_of(r.entry) - con['b0']; xx = cb.bar_of(r.exit) - con['b0']
            qe.append(con['qv'][xe]); qx.append(con['qv'][min(xx, len(con['qv'])-1)])
        tr['qv_in'] = np.round(qe); tr['qv_out'] = np.round(qx)
        print('=====', per, asset, 'sum net% (unlev)', round(tr['net%'].sum(), 2), 'spread', round(tr['spr%'].sum(), 2), 'fund', round(tr['fund%'].sum(), 2))
        print(tr[['con', 'entry', 'exit', 'why', 'sig_in', 'prem_in%', 'spr%', 'fund%', 'net%', 'qv_in', 'qv_out']].round(3).to_string())
