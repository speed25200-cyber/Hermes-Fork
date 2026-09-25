"""Block bootstrap (10-day blocks, 5000 draws) 90% CI of OOS Sharpe for the selected config and the ensembles
(hybrid2 daily files from ens_robust.py). -> boot.json"""
import json, numpy as np, pandas as pd
rng = np.random.default_rng(1)
out = {}
for tag in ('selected_1', 'IS_10', 'plateau_16', 'plateau_32'):
    try:
        r = pd.read_csv(f'oos_daily_{tag}.csv', index_col=0).iloc[:, 0].values
    except FileNotFoundError:
        continue
    n, b = len(r), 10
    S = []
    for _ in range(5000):
        st = rng.integers(0, n - b, n // b + 1)
        x = np.concatenate([r[s:s + b] for s in st])[:n]
        S.append(x.mean() / x.std(ddof=1) * np.sqrt(365))
    q = np.quantile(S, [0.05, 0.5, 0.95])
    out[tag] = dict(ci90=[float(q[0]), float(q[2])], median=float(q[1]), p_le_0=float(np.mean(np.array(S) <= 0)))
    print(tag, out[tag])
json.dump(out, open('boot.json', 'w'), indent=1)
