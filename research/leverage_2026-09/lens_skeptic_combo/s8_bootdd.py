"""Forward-looking drawdown risk: stationary block bootstrap (mean block 10 d) of paired daily (close return, intrabar
trough) of the combined account at scale L over a 608-day horizon (same length as the OOS), book as-is / haircut 50% /
DSR-excess, listing base and listing with worst-case stop fills. Reports P(intrabar max DD > 35%) and the 95th pct DD.
Also the same for the listing sleeve alone at coin cap w_l*L (the combination while the book stays flat).
-> s8_bootdd.json"""
import json, os, sys, numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
from sim_patch import sim, simulate2
from s0_repro import load, sharpe
from s2_boot import stat_boot_idx

cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
Dt = sim.Data('hybrid')
j = load().dropna(subset=['book', 'nl'])['2025-01-01':'2026-08-31']
S1 = json.load(open(os.path.join(HERE, 's1_haircut.json'))); KB = S1['scenarios_k']
W = 0.6030102397652416; WL = 1 - W
LEVS = [2, 2.5, 3, 3.5, 4]
rng = np.random.default_rng(7)
n = len(j); NB = 2000
IDX = [stat_boot_idx(n, 10, rng) for _ in range(NB)]

def dd_paths(rc, rw):
    rc = np.asarray(rc); rw = np.asarray(rw)
    res = np.empty(NB)
    for b, ii in enumerate(IDX):
        c = rc[ii]; w = rw[ii]
        C = np.cumprod(1 + c); prevC = np.r_[1.0, C[:-1]]
        peak_prev = np.maximum.accumulate(np.r_[1.0, C])[:-1]
        res[b] = max(np.max(1 - prevC * (1 + w) / peak_prev), np.max(1 - C / np.maximum.accumulate(C)))
    return res

out = {}
for lv in ('base', 'stopworst'):
    for L in LEVS:
        r = simulate2(Dt, cfg, '2025-01-01', '2026-09-01', L=WL * L, record=True, stop_worst=(lv == 'stopworst'))
        eqc = r['eq'].resample('D').last(); prev = eqc.shift(1).fillna(1.0)
        tr = (r['eq_worst'].resample('D').min() / prev - 1).fillna(0.0).reindex(j.index)
        rl = r['ret'].reindex(j.index); tl = np.minimum(tr, rl)
        d = dd_paths(rl, tl)
        out[f'listing_alone|{lv}|L{L}'] = dict(p_dd_gt35=float((d > 0.35).mean()), dd_p50=float(np.median(d)), dd_p95=float(np.percentile(d, 95)))
        for bn in ('as_is', 'haircut50_0.68', 'dsr_excess'):
            rb = j.book - KB[bn] * j.book_gross
            rc = L * W * rb + rl; rw = np.minimum(L * W * np.minimum(rb, 0) + tl, rc)
            d = dd_paths(rc, rw)
            out[f'{bn}|{lv}|L{L}'] = dict(p_dd_gt35=float((d > 0.35).mean()), dd_p50=float(np.median(d)), dd_p95=float(np.percentile(d, 95)))
df = pd.DataFrame(out).T
df.index = pd.MultiIndex.from_tuples([tuple(k.split('|')) for k in df.index], names=['book', 'listing', 'L'])
pd.set_option('display.width', 250)
print(df.round(3).unstack('L').to_string())
json.dump(out, open(os.path.join(HERE, 's8_bootdd.json'), 'w'), indent=1)
