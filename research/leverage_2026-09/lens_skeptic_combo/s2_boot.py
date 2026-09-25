"""(3) Confidence in the combined OOS Sharpe: stationary block bootstrap (paired days, mean block 5/10/20),
analytic PSR (Mertens SE), Lo (2002) autocorrelation-adjusted Sharpe, for the book as-is and haircut scenarios.
-> s2_boot.json"""
import json, os, numpy as np, pandas as pd
from scipy import stats
from s0_repro import load, sharpe, HERE

j = load().dropna(subset=['book', 'nl'])['2023-08-01':'2026-08-31']
OOS = slice('2025-01-01', '2026-08-31')
S1 = json.load(open(os.path.join(HERE, 's1_haircut.json')))
K = S1['scenarios_k']
W = 0.6030102397652416

def lo_sharpe(r, q=365):
    r = np.asarray(r, float); n = len(r); sr = r.mean() / r.std(ddof=1)
    L = int(min(np.floor(4 * (n / 100.0) ** (2.0 / 9.0)), n // 4, q - 1))
    rc = r - r.mean(); den = rc @ rc; f = 1.0
    for k in range(1, L + 1):
        f += 2 * (1 - k / (L + 1)) * (rc[k:] @ rc[:-k]) / den
    return float(sr * np.sqrt(q) / np.sqrt(max(f, 1.0))), float(f)

def stat_boot_idx(n, b, rng):
    p = 1.0 / b
    idx = np.empty(n, dtype=np.int64); idx[0] = rng.integers(n)
    jumps = rng.random(n) < p; starts = rng.integers(0, n, size=n)
    for t in range(1, n):
        idx[t] = starts[t] if jumps[t] else (idx[t - 1] + 1) % n
    return idx

def psr(r, bench_ann):
    r = np.asarray(r); n = len(r); sr = r.mean() / r.std(ddof=1); sk = stats.skew(r); ku = stats.kurtosis(r, fisher=False)
    sd = np.sqrt((1 - sk * sr + (ku - 1) / 4 * sr ** 2) / (n - 1))
    return float(stats.norm.cdf((sr - bench_ann / np.sqrt(365)) / sd)), float(sd * np.sqrt(365)), float(sk), float(ku)

rng = np.random.default_rng(12345)
NB = 5000
out = {}
series = {}
for nm in ('as_is', 'haircut50_0.68', 'dsr_excess', 'zero'):
    book = j.book - K[nm] * j.book_gross
    series[nm] = (W * book + (1 - W) * j.nl)[OOS].values
series['listing_alone'] = j.nl[OOS].values
series['book_alone_as_is'] = j.book[OOS].values
n = len(series['as_is'])
idxs = {b: [stat_boot_idx(n, b, rng) for _ in range(NB)] for b in (5, 10, 20)}
for nm, r in series.items():
    lo, f = lo_sharpe(r)
    p1, se, sk, ku = psr(r, 1.0)
    p15, _, _, _ = psr(r, 1.5)
    res = dict(sharpe=sharpe(r), lo_sharpe=lo, lo_factor=f, se_mertens=se, skew=sk, kurt=ku,
               p_true_below_1_analytic=1 - p1, p_true_below_1p5_analytic=1 - p15)
    for b, ii in idxs.items():
        s = np.array([sharpe(r[i]) for i in ii])
        res[f'boot_b{b}'] = dict(p05=float(np.percentile(s, 5)), p50=float(np.percentile(s, 50)), p95=float(np.percentile(s, 95)),
                                 p_below_1=float((s < 1.0).mean()), p_below_1p5=float((s < 1.5).mean()), p_below_0=float((s < 0).mean()))
    out[nm] = res
    print(nm, json.dumps({k: (round(v, 3) if isinstance(v, float) else {kk: round(vv, 3) for kk, vv in v.items()}) for k, v in res.items()}))
json.dump(out, open(os.path.join(HERE, 's2_boot.json'), 'w'), indent=1)
