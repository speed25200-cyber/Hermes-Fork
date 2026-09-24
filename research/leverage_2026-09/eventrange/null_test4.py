"""ORB and wick martingale check on an ARITHMETIC continuous-ish random walk (price itself is a martingale),
per-config t-stats, long vs short split."""
import numpy as np, pandas as pd, itertools
from kernels import orb_sim
from signals import sessions
rows = []
for seed in range(12):
    rng = np.random.default_rng(1000 + seed); n = 1440 * 365; sub = 60
    steps = rng.standard_normal((n, sub)) * 0.06 / np.sqrt(sub)
    vm = np.exp(rng.normal(0, 0.4, n // 1440 + 1)).repeat(1440)[:n]; steps *= vm[:, None]
    p = (1000 + np.cumsum(steps.ravel())).reshape(n, sub)
    c = p[:, -1]; o = np.concatenate([[1000.0], c[:-1]])
    h = np.maximum(p.max(1), o); l = np.minimum(p.min(1), o)
    t = (pd.Timestamp('2023-01-01').value // 10**6 + np.arange(n) * 60000).astype(np.int64)
    day = (t // 86_400_000).astype(np.int64); month = pd.to_datetime(t, unit='ms').month.values.astype(np.int64)
    d = dict(t=t, h=h, l=l, c=c, miss=np.zeros(n, np.int8))
    nd = len(np.unique(day[5000:]))
    for M in (15, 30, 60):
        arm, until, ex, orh, orl, width, clean = sessions(d, 'asia', M)
        for sm, k_tp in itertools.product((False, True), (0.0, 1.0, 2.0)):
            r = np.zeros(nd); tr = np.ones(nd); st = np.zeros(8, np.int64); trades = np.zeros(5000)
            k = orb_sim(o, h, l, c, h, l, c, np.zeros(n), np.zeros(n, np.int8), day, month, 5000, n, arm, until, ex, orh, orl, clean.astype(np.int64),
                        1e-9, 0.004, 0.0, 1.0, sm, k_tp, 0.0, 0.0, 0.0, 0.75, r, tr, st, trades)
            x = trades[:k]
            rows.append(dict(seed=seed, M=M, mid=sm, tp=k_tp, n=k, mean=x.mean(), sd=x.std()))
df = pd.DataFrame(rows)
g = df.groupby(['M', 'mid', 'tp']).apply(lambda z: pd.Series(dict(mean_bp=z['mean'].mean() * 1e4, t=z['mean'].mean() / (z['mean'].std() / np.sqrt(len(z))))), include_groups=False)
print(g.round(2))
per_seed = df.groupby('seed')['mean'].mean()
print('per-seed average over configs x1e4:', np.round(per_seed.values * 1e4, 2), 't across seeds %.2f' % (per_seed.mean() / per_seed.std() * np.sqrt(len(per_seed))))
