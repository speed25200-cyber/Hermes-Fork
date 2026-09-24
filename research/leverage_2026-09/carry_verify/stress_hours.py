import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify')
import carry_sim_v as cv, numpy as np, pandas as pd
P = cv.load(); M = cv.load_m1(); idx = P['s_c'].index
for ts in ['2023-01-14 00:00', '2023-10-23 22:00', '2026-08-19 15:00', '2025-03-02 18:00', '2026-02-03 20:00', '2023-11-10 03:00', '2022-09-15 06:00']:
    t = idx.get_loc(pd.Timestamp(ts, tz='UTC'))
    for j, c in enumerate(['BTCUSDT', 'ETHUSDT']):
        mk_c, ix_c, mk_h, ix_h = M['mk_c'][t][:, j], M['ix_c'][t][:, j], M['mk_h'][t][:, j], M['ix_h'][t][:, j]
        prev_prem = P['m_c'][c].iloc[t-1] / P['i_c'][c].iloc[t-1] - 1
        pc = mk_c / ix_c - 1; ph = mk_h / ix_h - 1
        print(ts, c[:3], f"prev_prem {prev_prem*1e4:6.1f}bp | 1h proxy jump {P['jump_mark'][c].iloc[t]*1e4:6.1f}bp | 1m max prem close {pc.max()*1e4:6.1f}bp (min {pc.argmax()}) high-high {ph.max()*1e4:6.1f}bp | idx move in hr {(ix_c[-1]/P['i_c'][c].iloc[t-1]-1)*100:5.2f}% max {(ix_h.max()/P['i_c'][c].iloc[t-1]-1)*100:5.2f}%")
