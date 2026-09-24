import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify')
import carry_sim_v as cv, pandas as pd, numpy as np
for L, band in [(10, .2), (15, .2), (20, .05)]:
    for per, (a, b) in {'IS': ('2022-01-01', '2024-12-31 23:00'), 'OOS': ('2025-01-01', '2026-08-31 23:00')}.items():
        for ib in ['proxy', '1m']:
            r = cv.sim(['BTCUSDT', 'ETHUSDT'], L, band=band, start=a, end=b, record=True, intrabar=ib)
            s = r['series']; m = s['mr_worst'].dropna()
            d = m.groupby(m.index.date).min().sort_values().head(5)
            dd = 1 - s['Emin'] / s['E'].cummax().shift(1).fillna(1).clip(lower=1)
            print(f'L={L} {per} {ib:5s} liq={r["liquidations"]} maxDD={r["maxdd"]:.3f} (worst intrabar at {dd.idxmax()}) lowest MR days:', ' '.join(f'{k}:{v:.2f}' for k, v in d.items()))
