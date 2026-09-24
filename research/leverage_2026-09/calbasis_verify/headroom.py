import pandas as pd, numpy as np
from load import load
import cb_v as cb
AS = load()
B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
pd.set_option('display.width', 250)
out = []
for per, (a, b) in {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}.items():
    for L in [10, 15, 20]:
        for asset in ['BTC', 'ETH']:
            s = cb.Sim(AS[asset], B, L, 'cross', start=a, end=b).run()
            h = pd.DataFrame(s.head); h['per'] = per; h['L'] = L; h['asset'] = asset
            out.append(h)
H = pd.concat(out)
H.to_csv('headroom.csv', index=False)
# closest approach per period/L/asset
g = H.loc[H.groupby(['per', 'L', 'asset']).head_eq.idxmin().values] if False else H.sort_values('head_eq').groupby(['per', 'L', 'asset']).head(2)
print(g.sort_values(['per', 'L', 'asset', 'head_eq'])[['per', 'L', 'asset', 'con', 'entry', 'tmin', 'head_eq', 'head_bp', 'eq_min']].round(3).to_string())
