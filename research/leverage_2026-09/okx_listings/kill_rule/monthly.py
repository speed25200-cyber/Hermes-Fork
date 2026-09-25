import sys
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve'); sys.path.insert(0, SP + '/newlisting')
import sim
import numpy as np, pandas as pd
from livesim import simulate_live
Db = sim.Data('hybrid')
r = simulate_live(Db, tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True, start='2025-01-01', end='2026-09-01')
tr = pd.DataFrame(r['trades'])
tr['m'] = (pd.Timestamp('2021-12-01') + pd.to_timedelta(tr.entry_t, unit='h')).dt.to_period('M')
g = tr.groupby('m').agg(trades=('ret', 'size'), tokens=('sym', 'nunique'), mean=('ret', 'mean'))
print(g.to_string())
