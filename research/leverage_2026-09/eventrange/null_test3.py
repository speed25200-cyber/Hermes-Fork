"""ORB martingale check on a near-continuous path (Gaussian sub-steps, 60 per bar): the +1bp/trade seen with
fat-tailed jumps should vanish if it came from stop orders filled at the level when the price jumped through it."""
import numpy as np, pandas as pd, itertools
from kernels import orb_sim
from signals import sessions
xs = []
for seed in range(8):
    rng = np.random.default_rng(100 + seed); n = 1440 * 365; sub = 60
    steps = rng.standard_normal((n, sub)) * 0.0006 / np.sqrt(sub)
    vm = np.exp(rng.normal(0, 0.4, n // 1440 + 1)).repeat(1440)[:n]; steps *= vm[:, None]
    lp = (np.log(100) + np.cumsum(steps.ravel())).reshape(n, sub)
    c = np.exp(lp[:, -1]); o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(np.exp(lp.max(1)), o); l = np.minimum(np.exp(lp.min(1)), o)
    t = (pd.Timestamp('2023-01-01').value // 10**6 + np.arange(n) * 60000).astype(np.int64)
    day = (t // 86_400_000).astype(np.int64); month = pd.to_datetime(t, unit='ms').month.values.astype(np.int64)
    d = dict(t=t, h=h, l=l, c=c, miss=np.zeros(n, np.int8))
    nd = len(np.unique(day[5000:]))
    for M in (15, 30, 60):
        arm, until, ex, orh, orl, width, clean = sessions(d, 'asia', M)
        for sm, k_tp in itertools.product((False, True), (0.0, 1.0, 2.0)):
            r = np.zeros(nd); tr = np.ones(nd); st = np.zeros(8, np.int64); trades = np.zeros(5000)
            k = orb_sim(o, h, l, c, h, l, c, np.zeros(n), np.zeros(n, np.int8), day, month, 5000, n, arm, until, ex, orh, orl, clean.astype(np.int64),
                        1e-6, 0.004, 0.0, 1.0, sm, k_tp, 0.0, 0.0, 0.0, 0.75, r, tr, st, trades)
            xs.append(trades[:k])
x = np.concatenate(xs)
print('orb continuous-path pooled obs', len(x), 'mean x1e4 %.2f' % (x.mean() * 1e4), 'naive t %.2f' % (x.mean() / x.std() * np.sqrt(len(x))))
