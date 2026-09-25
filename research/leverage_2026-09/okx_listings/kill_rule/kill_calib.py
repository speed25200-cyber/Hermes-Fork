"""Calibrate the sleeve's kill rule on the Binance-event live-rule replay: false-kill probability when the edge is as in
the honest OOS sample (2025-26, pre-market perps excluded), detection probability and speed when the edge is zero
(same trades demeaned). Trade return = net of BTC hedge, research costs and coin funding, per unit notional."""
import sys, json
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve'); sys.path.insert(0, SP + '/newlisting')
import sim
import numpy as np, pandas as pd
from livesim import simulate_live
Db = sim.Data('hybrid')
r = simulate_live(Db, tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True, start='2022-01-01', end='2026-09-01')
tr = pd.DataFrame(r['trades'])
pm = set(pd.read_csv(SP + '/xlist/verify/premkt/bn_premarket_classification.csv').query('premarket == True').i)
k0 = tr.entry_t.values - Db.g0[tr.i.values]
k1 = np.minimum(tr.exit_t.values - Db.g0[tr.i.values], Db.H - 1)
b0 = Db.bo[tr.i.values, k0]
b1 = np.where(np.isfinite(Db.bo[tr.i.values, k1]), Db.bo[tr.i.values, k1], Db.bc[tr.i.values, np.maximum(k1 - 1, 0)])
fund = np.array([np.nansum(Db.fund[i, a:b]) for i, a, b in zip(tr.i.values, k0, k1)])
tr['net'] = tr.ret.values + (b1 / b0 - 1) - 2 * (0.0015 + 0.0006) + fund
tr['pm'] = tr.i.isin(pm)
tr['year'] = (pd.Timestamp('2021-12-01') + pd.to_timedelta(tr.entry_t, unit='h')).dt.year
print(tr.groupby(['year', 'pm']).net.agg(['size', 'mean', 'std']).round(3).to_string())
oos = tr[(tr.year >= 2025) & ~tr.pm]
full = tr
groups = lambda d: [g.net.values for _, g in d.sort_values('entry_t').groupby('i', sort=False)]
H1 = groups(oos)
m = oos.net.mean()
H0 = [g - m for g in H1]
Hh = [g - m / 2 for g in H1]
Hf = groups(full)
rng = np.random.default_rng(7)
TOK = 72                                             # 24 months at ~3 new tokens a month


def first_kill(seq, N, thr, cum_thr):
    c = np.cumsum(np.insert(seq, 0, 0.0))
    for t in range(N, len(seq) + 1):
        mean_n = (c[t] - c[t - N]) / N
        if mean_n <= thr:
            return t
        if cum_thr is not None and c[t] - c[:t + 1].max() <= cum_thr:
            return t
    return None


paths = {name: [np.concatenate([G[j] for j in rng.integers(0, len(G), TOK)]) for _ in range(3000)]
         for name, G in (('edge_oos', H1), ('edge_half', Hh), ('edge_zero', H0), ('full_incl_pm', Hf))}
rows = []
for N in (25, 30, 40, 50, 60):
    for thr in (0.0, -0.01, -0.02, -0.03, -0.05):
        rec = dict(N=N, thr=thr)
        for name, ps in paths.items():
            ks = [first_kill(p, N, thr, None) for p in ps]
            rec[name] = round(float(np.mean([k is not None for k in ks])), 3)
            if name == 'edge_zero':
                done = [k for k in ks if k is not None]
                rec['zero_median_trades'] = int(np.median(done)) if done else None
        rows.append(rec)
res = pd.DataFrame(rows)
print('per-trade net mean OOS post-launch %.4f sd %.4f n %d tokens %d' % (m, oos.net.std(), len(oos), len(H1)))
print(res.to_string(index=False))
res.to_csv('kill_calib.csv', index=False)
