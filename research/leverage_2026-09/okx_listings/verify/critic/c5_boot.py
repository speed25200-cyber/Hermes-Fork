"""Paired 21-day block bootstrap of Delta-Sharpe (union variant minus Binance-only variant) on daily returns."""
import numpy as np, pandas as pd, json
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
D = pd.read_pickle(SP + '/xlist/verify/critic/c1_daily.pkl')
def sr(x): return x.mean() / x.std(ddof=1) * np.sqrt(365)
rng = np.random.default_rng(7)
out = {}
for u, b in (('union', 'bn_only'), ('union_dedupe168', 'bn_only'), ('union_exPM', 'bn_only_exPM'), ('union_exPM_dedupe168', 'bn_only_exPM')):
    for p in ('2025-26', '2026JanAug', '2022-26'):
        x, y = D[f'{u}|{p}'].values, D[f'{b}|{p}'].values
        n = len(x); L = 21; nb = int(np.ceil(n / L))
        d0 = sr(x) - sr(y); bs = []
        for _ in range(2000):
            st = rng.integers(0, n - L + 1, nb)
            idx = (st[:, None] + np.arange(L)).ravel()[:n]
            bs.append(sr(x[idx]) - sr(y[idx]))
        bs = np.array(bs); se = bs.std()
        out[f'{u} vs {b} {p}'] = dict(dSR=round(d0, 3), se=round(se, 3), z=round(d0 / se, 2), p_le0=round(float((bs - bs.mean() + d0 <= 0).mean()), 3))
        print(f'{u:22s} vs {b:13s} {p:11s} dSR {d0:+.3f} SE {se:.3f} z {d0/se:+.2f}')
json.dump(out, open(SP + '/xlist/verify/critic/c5_boot.json', 'w'), indent=1)
