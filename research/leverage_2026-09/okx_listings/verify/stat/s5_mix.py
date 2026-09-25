"""Sharpe-optimal mix of the standalone OKX-extra sleeve with the Binance-only sleeve (daily returns from s1), and the
Sharpe gain it implies: SR* = sqrt(SR_B^2 + (SR_O - rho SR_B)^2 / (1 - rho^2)). Also vol-matched comparison.
-> s5_mix.json"""
import json, numpy as np, pandas as pd
from statlib import sharpe, OUT
d = pd.read_parquet(f'{OUT}/s1_daily.parquet')
res = {}
for pn in ('2022-2024', '2025-2026', '2026 Jan-Aug', '2022-2026'):
    b = d[f'binance_only | {pn}'].dropna().values
    for v in ('union', 'okx_nobn', 'okx_exit_bn24'):
        o = d[f'{v}|alone | {pn}'].dropna().values
        n = min(len(b), len(o)); b_, o_ = b[:n], o[:n]
        sb, so = sharpe(b_), sharpe(o_)
        rho = np.corrcoef(b_, o_)[0, 1]
        srs = np.sqrt(sb ** 2 + (so - rho * sb) ** 2 / (1 - rho ** 2)) if so > rho * sb else sb
        mu = np.array([b_.mean(), o_.mean()]); S = np.cov(np.vstack([b_, o_]))
        w = np.linalg.solve(S, mu); w = w / w[0]
        u = d[f'{v} | {pn}'].dropna().values
        res[f'{v} | {pn}'] = dict(sr_bn=round(sb, 3), sr_okx_alone=round(so, 3), rho=round(rho, 3), sr_optimal_mix=round(float(srs), 3),
                                  gain_at_optimum=round(float(srs - sb), 3), opt_weight_okx_per_unit_bn=round(float(w[1]), 3),
                                  vol_bn=round(float(b_.std() * np.sqrt(365)), 3), vol_okx=round(float(o_.std() * np.sqrt(365)), 3),
                                  vol_union=round(float(u.std() * np.sqrt(365)), 3), sr_union=round(sharpe(u), 3))
        print(pn, v, res[f'{v} | {pn}'])
json.dump(res, open(f'{OUT}/s5_mix.json', 'w'), indent=1)
