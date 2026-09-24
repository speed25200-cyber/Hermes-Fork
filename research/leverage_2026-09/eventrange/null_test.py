"""Martingale check: on a driftless synthetic random walk (1m bars built from 20 sub-steps, fat-tailed jumps), with
zero fees/slippage, every strategy's expected P&L must be ~0 or negative (conservative fill rules); a clearly positive
mean would reveal look-ahead in the kernels."""
import numpy as np, pandas as pd, itertools
from kernels import wick_sim, grid_sim, orb_sim
from signals import wick_events, sessions, narrow_flag
rng = np.random.default_rng(7)
n = 1440 * 365
sub = 20
steps = rng.standard_t(3, size=(n, sub)) * 0.0006 / np.sqrt(sub) / np.sqrt(3)
# volatility clustering: daily vol multiplier
vm = np.exp(rng.normal(0, 0.4, n // 1440 + 1)).repeat(1440)[:n]
steps *= vm[:, None]
lp = np.log(100) + np.cumsum(steps.ravel())
lp = lp.reshape(n, sub)
c = np.exp(lp[:, -1]); o = np.concatenate([[100.0], c[:-1]])
h = np.maximum(np.exp(lp.max(1)), o); l = np.minimum(np.exp(lp.min(1)), o)
t = (pd.Timestamp('2023-01-01').value // 10**6 + np.arange(n) * 60000).astype(np.int64)
d = dict(t=t, o=o, h=h, l=l, c=c, mh=h.copy(), ml=l.copy(), mc=c.copy(), fund=np.zeros(n), fflag=np.zeros(n, np.int8),
         qv=rng.lognormal(0, 1, n) * (1 + 50 * np.abs(np.log(c / o))), miss=np.zeros(n, np.int8))
d['day'] = (t // 86_400_000).astype(np.int64); ts = pd.to_datetime(t, unit='ms'); d['month'] = (ts.year * 12 + ts.month).values.astype(np.int64)
args = (d['o'], d['h'], d['l'], d['c'], d['mh'], d['ml'], d['mc'], d['fund'], d['fflag'], d['day'], d['month'], 5000, n)
nd = len(np.unique(d['day'][5000:]))
res = []
for w, z, V in itertools.product((1, 5), (6, 10), (3,)):
    idx, dr, mf = wick_events(d, w, z, V)
    for e, f, s, H in itertools.product((False, True), (0.3, 0.6), (0.5, 1.0), (30, 240)):
        r = np.zeros(nd); tr = np.ones(nd); st = np.zeros(8, np.int64); trades = np.zeros(50000)
        k = wick_sim(*args, idx, dr, mf, 0.0001, 0.004, 0.0, 1.0, e, f, s, H, False, 30, 0.25, 15, 0.0, 0.0, 0.0, 0.75, r, tr, st, trades)
        res.append(('wick', trades[:k].mean(), k))
for g, N, W, b in itertools.product((0.005, 0.01, 0.02), (5, 10), (0.0, 24.0), (True, False)):
    r = np.zeros(nd); tr = np.ones(nd); st = np.zeros(8, np.int64)
    grid_sim(*args, 0.0001, 0.004, 0.0, 1.0, g, N, W, b, 0.0, 0.0, 0.0, r, tr, st)
    res.append(('grid', r.mean(), int(st[0])))
for M in (15, 30, 60):
    arm, until, ex, orh, orl, width, clean = sessions(d, 'asia', M)
    for sm, k_tp in itertools.product((False, True), (0.0, 1.0, 2.0)):
        r = np.zeros(nd); tr = np.ones(nd); st = np.zeros(8, np.int64); trades = np.zeros(5000)
        k = orb_sim(*args, arm, until, ex, orh, orl, clean.astype(np.int64), 0.0001, 0.004, 0.0, 1.0, sm, k_tp, 0.0, 0.0, 0.0, 0.75, r, tr, st, trades)
        res.append(('orb', trades[:k].mean(), k))
df = pd.DataFrame(res, columns=['fam', 'mean', 'n'])
for fam, g in df.groupby('fam'):
    print(fam, 'configs', len(g), 'mean of per-trade (wick/orb) or per-day (grid) returns x1e4:', np.round(g['mean'].values * 1e4, 2))
