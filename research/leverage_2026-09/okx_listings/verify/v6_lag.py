"""Entry/exit one hour later for OKX events whose first trade is >= 1 min after the floored hour (live decides on
hourly bar closes against the exact launch time)."""
import json
from v_common import *
import indep
Do = load_okx()
m = (Do.ev.t0_exact - Do.ev.t0).values / 60000
lag = (m >= 1).astype(int)
print('events lagged', int(lag.sum()), 'of', Do.n)
out = {}
for pn, a, b in [('2022-2026', '2022-01-01', '2026-09-25'), ('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-25'), ('2026', '2026-01-01', '2026-09-25')]:
    r0 = indep.sim(Do, a, b); r1 = indep.sim(Do, a, b, lag=lag)
    out[pn] = dict(base=round(sharpe(r0['ret']), 3), lag1h=round(sharpe(r1['ret']), 3), mean_base=round(r0['trades'].ret.mean(), 4), mean_lag=round(r1['trades'].ret.mean(), 4), n_lag=len(r1['trades']))
    print(pn, out[pn])
json.dump(out, open(SP + '/xlist/verify/v6_lag.json', 'w'), indent=1)
