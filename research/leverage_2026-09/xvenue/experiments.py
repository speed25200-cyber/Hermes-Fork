"""IS-only parameter selection, then OOS evaluation, for the Binance-vs-OKX cross-venue arbitrage.

Variant F (funding-differential carry):
  Stage A  (IS 2022-01-01..2024-12-31, L=1): signal grid (H, th_in, th_out, K, universe, balance).
  Stage B  (IS, each L in 1,3,5,10,15,20,30,40): up to 8 signal configs from A x risk grid (R, k_dl, delta);
           pick max IS CAGR per L.
  Final    : the chosen config per L run on IS and on OOS (2025-01-01..2026-08-31, fresh 100k account) + sensitivities.
Variant S (price-divergence reversion): same protocol with its own grid.
Nothing in Stage A/B ever looks at OOS.
"""
import os, sys, json, itertools, time
import numpy as np, pandas as pd
from multiprocessing import Pool
import xvenue_bt as bt

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, os.environ.get('XV_OUT', 'results'))
os.makedirs(OUT, exist_ok=True)
IS = dict(start='2022-01-01', end='2025-01-01')
OOS = dict(start='2025-01-01', end='2026-09-01')
LEVS = [1, 3, 5, 10, 15, 20, 30, 40]
SIGF = ['H', 'th_in', 'th_out', 'K', 'universe', 'balance']
SIGS = ['strategy', 'sp_in', 'sp_out', 'K', 'universe', 'sp_maxhold']


def _run(params):
    r = bt.run(params)
    r = {k: v for k, v in r.items() if not k.startswith('_')}
    r['params'] = params
    return r


def pmap(jobs, n=4):
    bt.load_panel()
    with Pool(n) as pool:
        return pool.map(_run, jobs, chunksize=1)


def flat(r):
    d = dict(r['params'])
    for k in ['cagr', 'maxdd', 'worst_day', 'sharpe', 'final', 'liq', 'fills', 'entries', 'dlev', 'cuts', 'transfers',
              'realize', 'fees_pct', 'slip_pct', 'funding_pct', 'liqloss_pct']:
        d[k] = r[k]
    for k in r:
        if k.startswith('cost_'):
            d[k] = r[k]
    for y, v in r['yearly'].items():
        d['y' + y] = v
    return d


def stage_a():
    grid = []
    for H, thi, ratio in itertools.product([72, 168, 336, 720], [0.05, 0.10, 0.20, 0.35], [0.25, 0.5]):
        for uni, Ks in [('btceth', [1, 2]), ('majors', [2, 4, 6, 10]), ('all', [2, 4, 6, 10])]:
            for K in Ks:
                for bal in ([False, True] if K >= 2 else [False]):
                    grid.append(dict(IS, L=1.0, H=H, th_in=thi, th_out=thi * ratio, K=K, universe=uni, balance=bal))
    t = time.time()
    res = pmap(grid)
    df = pd.DataFrame([flat(r) for r in res]).sort_values('cagr', ascending=False)
    df.to_csv(os.path.join(OUT, 'stageA_IS_L1.csv'), index=False)
    print('stage A', len(grid), 'runs', round(time.time() - t), 's', flush=True)
    print(df.head(15)[SIGF + ['cagr', 'maxdd', 'sharpe', 'entries', 'funding_pct', 'fees_pct', 'slip_pct']].to_string(), flush=True)
    return df


def pick_signals(dfa):
    d = dfa.drop_duplicates(SIGF)
    sel = d.head(4)[SIGF].to_dict('records')
    for sub in [d.sort_values('sharpe', ascending=False).head(2), d[d.balance == True].head(1),
                d[d.universe == 'btceth'].head(1), d[d.universe == 'majors'].head(1)]:
        for x in sub[SIGF].to_dict('records'):
            if x not in sel:
                sel.append(x)
    for x in sel:
        x['K'] = int(x['K']); x['H'] = int(x['H']); x['balance'] = bool(x['balance'])
    return sel


def stage_b(dfa):
    tops = pick_signals(dfa)
    print('signal configs for stage B:', tops, flush=True)
    grid = []
    for L in LEVS:
        for s, R, kdl, dl in itertools.product(tops, [1, 4], [2.0, 3.0, 5.0, 8.0, 12.0], [0.25, 0.5]):
            grid.append(dict(IS, L=float(L), R=R, k_dl=kdl, delta=dl, **s))
    t = time.time()
    res = pmap(grid)
    df = pd.DataFrame([flat(r) for r in res])
    df.to_csv(os.path.join(OUT, 'stageB_IS.csv'), index=False)
    print('stage B', len(grid), 'runs', round(time.time() - t), 's', flush=True)
    best = df.sort_values('cagr', ascending=False).groupby('L').head(1).sort_values('L')
    print(best[['L', 'R', 'k_dl', 'delta'] + SIGF + ['cagr', 'maxdd', 'sharpe', 'liq', 'cuts']].to_string(), flush=True)
    return df, best


def stage_spread():
    grid = []
    for spi, spo, K, uni, mh in itertools.product([30, 50, 80, 120], [5, 15], [1, 3], ['majors', 'all'], [24, 72]):
        grid.append(dict(IS, L=1.0, strategy='spread', sp_in=float(spi), sp_out=float(spo), K=K, universe=uni,
                         sp_maxhold=mh, sp_lag=1))
    res = pmap(grid)
    dfa = pd.DataFrame([flat(r) for r in res]).sort_values('cagr', ascending=False)
    dfa.to_csv(os.path.join(OUT, 'spread_stageA_IS_L1.csv'), index=False)
    print(dfa.head(8)[SIGS + ['cagr', 'maxdd', 'sharpe', 'entries']].to_string(), flush=True)
    tops = dfa.drop_duplicates(SIGS).head(3)[SIGS].to_dict('records')
    for x in tops:
        x['K'] = int(x['K']); x['sp_maxhold'] = int(x['sp_maxhold'])
    grid = []
    for L in LEVS:
        for s, R, kdl in itertools.product(tops, [1, 4], [2.0, 5.0, 12.0]):
            grid.append(dict(IS, L=float(L), R=R, k_dl=kdl, sp_lag=1, **s))
    res = pmap(grid)
    dfb = pd.DataFrame([flat(r) for r in res])
    dfb.to_csv(os.path.join(OUT, 'spread_stageB_IS.csv'), index=False)
    best = dfb.sort_values('cagr', ascending=False).groupby('L').head(1).sort_values('L')
    return best


def final(best, keys, fname, sens=True):
    chosen = []
    for _, r in best.iterrows():
        c = {}
        for k in keys:
            v = r[k]
            if k in ('K', 'R', 'H', 'sp_maxhold', 'sp_lag'):
                v = int(v)
            elif k == 'balance':
                v = bool(v)
            elif isinstance(v, (np.floating,)):
                v = float(v)
            c[k] = v
        chosen.append(c)
    jobs, tags = [], []
    for c in chosen:
        for per, pr in [('IS', IS), ('OOS', OOS)]:
            jobs.append(dict(c, **pr)); tags.append(('base', per, c['L']))
            if not sens:
                continue
            for dd in [1, 12, 24]:
                jobs.append(dict(c, D=dd, **pr)); tags.append((f'delay{dd}h', per, c['L']))
            jobs.append(dict(c, liq_price='last', **pr)); tags.append(('liq_on_last_price', per, c['L']))
            jobs.append(dict(c, liq_mode='sum', **pr)); tags.append(('liq_sum_of_worst', per, c['L']))
            jobs.append(dict(c, risk_mode='hourly', **pr)); tags.append(('hourly_risk_checks', per, c['L']))
            jobs.append(dict(c, gap_frac=0.1, **pr)); tags.append(('crash_gap_on_cuts', per, c['L']))
            jobs.append(dict(c, fee_maker_leg=0.0002, **pr)); tags.append(('okx_leg_maker', per, c['L']))
            jobs.append(dict(c, slip_mult=2.0, **pr)); tags.append(('slippage_x2', per, c['L']))
    res = pmap(jobs)
    rows = []
    for (tag, per, L), r in zip(tags, res):
        d = flat(r)
        d.update(variant=tag, period=per, liq_events=json.dumps(r['liq_events'][:5]))
        rows.append(d)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, fname), index=False)
    return df


if __name__ == '__main__':
    stage = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if stage in ('all', 'funding'):
        dfa = stage_a()
        dfb, best = stage_b(dfa)
        df = final(best, ['L', 'R', 'k_dl', 'delta'] + SIGF, 'final_runs.csv')
        print(df[df.variant == 'base'][['period', 'L', 'cagr', 'maxdd', 'worst_day', 'sharpe', 'liq', 'cuts', 'entries']].to_string(), flush=True)
    if stage in ('all', 'spread'):
        best = stage_spread()
        df = final(best, ['L', 'R', 'k_dl', 'sp_lag'] + SIGS, 'spread_final_runs.csv', sens=False)
        print(df[['period', 'L', 'cagr', 'maxdd', 'worst_day', 'sharpe', 'liq', 'cuts', 'entries']].to_string(), flush=True)
