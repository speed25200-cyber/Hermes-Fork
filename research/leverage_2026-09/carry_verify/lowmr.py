import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry')
import carry_sim as cs, pandas as pd, numpy as np
P = cs.load()
for L, band in [(10, .2), (15, .2), (20, .05)]:
    for per, (a, b) in {'IS': ('2022-01-01', '2024-12-31 23:00'), 'OOS': ('2025-01-01', '2026-08-31 23:00')}.items():
        s = cs.sim(['BTCUSDT', 'ETHUSDT'], L, band=band, start=a, end=b, record=True)['series']
        m = s['mr_worst'].dropna()
        d = m.groupby(m.index.date).min().sort_values().head(6)
        print(L, per, ' '.join(f'{k}:{v:.2f}' for k, v in d.items()))
for c in ['BTCUSDT', 'ETHUSDT']:
    j = P['jump_mark'][c]
    print(c, 'top mark-premium jumps (1h proxy):')
    print(j.sort_values(ascending=False).head(8).round(4).to_string())
