"""Sensitivity: OKX funding = Binance funding - 0.8%/yr (the gap measured over the only overlap, Jun-Aug 2026). 1m intrabar check, IS-selected bands."""
import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify')
import carry_sim_v as cv, pandas as pd, numpy as np
cv.load(); P = cv._cache
f = P['fund']
P['fund'] = f.where(f == 0, f - 0.008 / 1095)
bands = {1: 0.2, 3: 0.2, 5: 0.2, 8: 0.2, 10: 0.2, 15: 0.2, 20: 0.05}
rows = []
for L in [1, 3, 5, 8, 10, 15, 20]:
    r = cv.sim(['BTCUSDT', 'ETHUSDT'], L, band=bands[L], start='2025-01-01', end='2026-08-31 23:00', intrabar='1m')
    rows.append(dict(L=L, cagr_oos=round(r['cagr'], 4), maxdd_oos=round(r['maxdd'], 4), liq=r['liquidations']))
df = pd.DataFrame(rows); df.to_csv('okx_funding_haircut_oos.csv', index=False); print(df.to_string())
