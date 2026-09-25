"""Reproduce author's OKX-only and union/Binance-only runs; dump traded Binance events for pre-market checks."""
import sys, json, types
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve'); sys.path.insert(0, SP + '/newlisting')
import sim
import numpy as np, pandas as pd
from livesim import simulate_live, sharpe
HERE = SP + '/xlist/verify/premkt'
Db = sim.Data('hybrid'); Db.sigma_ref = 0.1238230231575359
r = simulate_live(Db, tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True, start='2022-01-01', end='2026-09-01')
tr = pd.DataFrame(r['trades'])
print('binance-only full-H sharpe', round(sharpe(r['ret']), 3), len(tr))
ev = Db.ev.copy(); ev['i'] = np.arange(len(ev)); ev['newtok'] = Db.newtok
tr = tr.merge(ev[['i', 'base', 't0', 'spot_first']], on='i')
tr.to_csv(HERE + '/bn_trades_base.csv', index=False)
u = tr.drop_duplicates('i')[['i', 'sym', 'base', 't0', 'spot_first']]
u.to_csv(HERE + '/bn_traded_events.csv', index=False)
print('traded binance events', len(u))
