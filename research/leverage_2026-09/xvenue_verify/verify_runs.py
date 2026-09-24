"""Adversarial robustness runs on the researcher's engine (xvenue_bt.py, unmodified; data modified in memory only).
mods: base | exAXS (AXS removed from universe) | lagN (funding signal delayed N hours) | spr_delay1 (spread signal
observed 1 bar earlier -> no same-bar signal/fill) | exAXS+... combos."""
import sys, json, os, time
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue')
import numpy as np, pandas as pd
from multiprocessing import Pool
import xvenue_bt as bt
IS = dict(start='2022-01-01', end='2025-01-01'); OOS = dict(start='2025-01-01', end='2026-09-01')
R = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/results/'
S = pd.read_csv(R + 'summary.csv')
_PM = {}

def getP(mod, H):
    key = (mod, H)
    if key in _PM:
        return _PM[key]
    P0 = bt.load_panel()
    P = dict(P0)
    for m in mod.split('+'):
        if m.startswith('ex'):
            coins = m[2:].split(',')
            P['valid'] = P['valid'].copy()
            for c in coins:
                P['valid'][:, P0['coins'].index(c)] = False
    D = bt.signal(P0, H).copy()
    for m in mod.split('+'):
        if m.startswith('lag'):
            n = int(m[3:]); D = np.vstack([np.full((n, D.shape[1]), np.nan), D[:-n]])
    bt._CACHE[('sig', H, id(P))] = D
    spr = bt.spread_dev(P0).copy()
    if 'spr_delay1' in mod.split('+'):
        spr = np.vstack([np.full((1, spr.shape[1]), np.nan), spr[:-1]])
    bt._CACHE[('spr', 24 * 7, id(P))] = spr
    _PM[key] = P
    return P

def _run(job):
    mod, p = job
    P = getP(mod, p.get('H', 72))
    r = bt.run(p, P=P)
    d = dict(mod=mod, **{k: p.get(k) for k in ['L', 'start', 'strategy', 'k_dl', 'risk_mode', 'H', 'th_in', 'th_out', 'K', 'universe', 'balance', 'R', 'delta']})
    d.update(cagr=r['cagr'], maxdd=r['maxdd'], worst_day=r['worst_day'], sharpe=r['sharpe'], liq=r['liq'], cuts=r['cuts'], entries=r['entries'],
             funding=r['funding_pct'], costs=r['fees_pct'] + r['slip_pct'])
    d.update({'y' + k: v for k, v in r['yearly'].items()})
    return d

def chosen(fam):
    out = []
    for _, row in S[S.family == fam].iterrows():
        c = json.loads(row['config']); c['L'] = float(row['L_per_venue'])
        if fam == 'price_divergence_reversion':
            c['strategy'] = 'spread'; c['sp_lag'] = 1
        if c['L'] <= 20:
            out.append(c)
    return out

if __name__ == '__main__':
    what = sys.argv[1]
    jobs = []
    if what == 'robust':
        for c in chosen('funding_diff_carry'):
            for mod in ['base', 'exAXS', 'lag8', 'lag24', 'exAXS+lag8']:
                for pr in (IS, OOS):
                    jobs.append((mod, dict(c, **pr)))
            for mod in ['base', 'exAXS']:
                for pr in (IS, OOS):
                    jobs.append((mod, dict(c, risk_mode='hourly', **pr)))
        for c in chosen('price_divergence_reversion'):
            for mod in ['base', 'spr_delay1']:
                for pr in (IS, OOS):
                    jobs.append((mod, dict(c, **pr)))
    elif what == 'neigh':
        B = pd.read_csv(R + 'stageB_IS.csv')
        keys = ['L', 'R', 'k_dl', 'delta', 'H', 'th_in', 'th_out', 'K', 'universe', 'balance']
        for _, r in B[B.L.isin([5.0, 10.0, 15.0, 20.0])].iterrows():
            c = {k: r[k] for k in keys}
            for k in ('R', 'H', 'K'): c[k] = int(c[k])
            c['balance'] = bool(c['balance']); c['L'] = float(c['L']); c['k_dl'] = float(c['k_dl']); c['delta'] = float(c['delta'])
            c['th_in'] = float(c['th_in']); c['th_out'] = float(c['th_out'])
            jobs.append(('base', dict(c, **OOS)))
            jobs.append(('exAXS', dict(c, **OOS)))
    elif what == 'loo':
        coins = bt.load_panel()['coins']
        for c in chosen('funding_diff_carry'):
            if c['L'] not in (1.0, 5.0, 10.0, 15.0):
                continue
            for co in coins:
                jobs.append(('ex' + co, dict(c, **OOS)))
    t = time.time()
    bt.load_panel()
    with Pool(4) as pool:
        res = pool.map(_run, jobs, chunksize=1)
    df = pd.DataFrame(res)
    df['period'] = np.where(df.start == '2022-01-01', 'IS', 'OOS')
    df.to_csv(f'verify_{what}.csv', index=False)
    print(len(jobs), 'runs', round(time.time() - t), 's')
