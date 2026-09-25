"""Minimum paper/live track record (days) for PSR(true SR > benchmark) >= 90% / 95% if the combination's true Sharpe is
as-is or haircut (Bailey & Lopez de Prado min-TRL with the combination's own OOS skew/kurtosis). -> s9_mintrack.json"""
import json, os, numpy as np
from scipy import stats
from s0_repro import load, HERE
j = load().dropna(subset=['book', 'nl'])['2025-01-01':'2026-08-31']
K = json.load(open(os.path.join(HERE, 's1_haircut.json')))['scenarios_k']; W = 0.6030102397652416
out = {}
for nm in ('as_is', 'haircut50_0.68', 'dsr_excess'):
    r = (W * (j.book - K[nm] * j.book_gross) + (1 - W) * j.nl).values
    sr = r.mean() / r.std(ddof=1); sk = stats.skew(r); ku = stats.kurtosis(r, fisher=False)
    for b in (0.0, 0.5, 1.0):
        for conf in (0.9, 0.95):
            z = stats.norm.ppf(conf); d = sr - b / np.sqrt(365)
            out[f'{nm}|bench{b}|{conf}'] = float(1 + (1 - sk * sr + (ku - 1) / 4 * sr ** 2) * (z / d) ** 2) if d > 0 else None
print(json.dumps({k: (round(v) if v else v) for k, v in out.items()}, indent=0))
json.dump(out, open(os.path.join(HERE, 's9_mintrack.json'), 'w'), indent=1)
