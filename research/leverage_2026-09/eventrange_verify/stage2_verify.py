"""Verifier: re-run the post-hoc stage-2 dip-buy configs (same 12 configs as stage2.py) under (a) the original rules and
(b) the optimistic bound (OPT/NORS env), then leave-one-coin-out and leave-one-month-out on the configurations that
are positive in both windows at L>=3. Usage: OPT=0 NORS=0 python stage2_verify.py"""
import sys, os, json, itertools
sys.argv = ['run_opt.py', 'main']
import numpy as np, pandas as pd
import run_opt as run
from common import COINS, combine, metrics
from multiprocessing import Pool
CFGS = [dict(w=5, z=10, V=V, entry='mkt', tp=1e6, stop=s, hold=H, side='long')
        for V, H, s in itertools.product((3, 10), (15, 60, 240), (1e6, 2.0))]

def port(cfg, L, per, drop_coin=None):
    sl = []
    for coin in COINS:
        if coin == drop_coin:
            continue
        r, tr, st, _ = run.run_sleeve('wick', cfg, coin, L, per)
        sl.append((r, tr))
    V, TR = combine(sl, None, run.MONTHS[per])
    return V, TR

def cagr_from_daily(dr, nd):
    v = np.prod(1 + dr)
    return v ** (365.25 / nd) - 1 if v > 0 else -1.0

def lomo(V, per):
    prevV = np.concatenate([[1.0], V[:-1]]); dr = np.where(prevV > 0, V / prevV - 1, 0.0)
    mo = run.MONTHS[per]; out = {}
    for m in np.unique(mo):
        x = dr.copy(); x[mo == m] = 0.0
        out[int(m)] = cagr_from_daily(x, len(x))
    return out

def job(a):
    k, cfg = a
    res = []
    for L in (1, 3, 5, 10):
        for per in ('IS', 'OOS'):
            V, TR = port(cfg, L, per)
            m = metrics(V, TR, run.DAYS[per])
            lm = lomo(V, per)
            loco = {c: metrics(*port(cfg, L, per, c), run.DAYS[per])['cagr'] for c in COINS}
            mmin = min(lm, key=lm.get)
            res.append(dict(k=k, cfg=f"V{cfg['V']} H{cfg['hold']} stop{'liqcap' if cfg['stop'] > 100 else cfg['stop']}", L=L, period=per,
                            cagr=m['cagr'], maxdd=m['maxdd'], sharpe=m['sharpe'],
                            lomo_min_cagr=lm[mmin], lomo_worst_month=f'{(mmin - 1) // 12}-{(mmin - 1) % 12 + 1:02d}',
                            loco_min_cagr=min(loco.values()), loco_worst_coin=min(loco, key=loco.get)))
    return res

if __name__ == '__main__':
    with Pool(4) as pool:
        rr = pool.map(job, list(enumerate(CFGS)))
    df = pd.DataFrame([r for x in rr for r in x])
    tag = f"opt{run.OPT}_nors{int(run.NORS)}"
    df.to_csv(f'out/verify_stage2_{tag}.csv', index=False)
    p = df.pivot_table(index=['cfg', 'L'], columns='period', values=['cagr', 'maxdd', 'lomo_min_cagr', 'loco_min_cagr'])
    p.columns = [f'{a}_{b}' for a, b in p.columns]
    pd.set_option('display.width', 250)
    print(tag); print(p.round(3).to_string())
