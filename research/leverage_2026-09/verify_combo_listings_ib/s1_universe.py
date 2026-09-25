"""Point-in-time book universe (Hermes rules: top 30 by 30d trailing quote volume, >= 60 days history, OKX-listed on D-1,
weekly reselection) from Binance daily klines, via Hermes' own daily_membership (read-only import).
-> universe_daily.parquet (day x sym bool, 2024-12..2026-08) and list of (sym, month) 1h files needed."""
import sys, pickle
sys.path.insert(0, '/home/user/Hermes/src')
import numpy as np, pandas as pd
from hermes.data.universe import daily_membership
from hermes.config import UniverseConfig
d = pd.read_parquet('../newlisting/data/daily_klines.parquet')
d['date'] = pd.to_datetime(d.t, unit='ms', utc=True)
qv = d.pivot_table(index='date', columns='sym', values='qv')
qv = qv.loc['2022-01-01':'2026-08-31']
alive = qv.notna() & (qv > 0)
listed = pd.read_parquet('../okx_listed.parquet')
cfg = UniverseConfig(top_n=30, liquidity_lookback_days=30, min_history_days=60, reselect_every_days=7, quote='USDT',
                     exclude=['USDCUSDT', 'BTCDOMUSDT', 'DEFIUSDT', 'FDUSDUSDT', 'TUSDUSDT'], venue='okx')
m = daily_membership(qv.fillna(0), alive, cfg, listed=listed)
m = m.loc['2024-12-01':'2026-08-31']
m = m.loc[:, m.any()]
print('members per day', m.sum(1).describe().round(1).to_dict())
print('distinct names 2024-12..2026-08', m.shape[1])
m.to_parquet('universe_daily.parquet')
h = pd.read_parquet('../verify_combo_tsmom/h1/h1_oos.parquet')
h['month'] = pd.to_datetime(h.t, unit='ms').dt.to_period('M').astype(str)
have = set(zip(h.sym, h.month))
need = set()
for day, row in m.iterrows():
    for s in row.index[row.values]:
        mo = str(day.to_period('M'))
        if (s, mo) not in have:
            need.add((s, mo))
print('sym-months needed', len(need), sorted({s for s, _ in need}))
pickle.dump(sorted(need), open('need_h1.pkl', 'wb'))
