"""Run the full study: IS selection (2022-01-01..2024-12-31), OOS report (2025-01-01..2026-08-31),
full-period per-year returns, sensitivities, crash windows. Writes results.csv / results.json."""
import json, itertools, time
import numpy as np, pandas as pd
from multiprocessing import Pool
import carry_sim as cs

OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry'
IS = ('2022-01-01', '2024-12-31 23:00')
OOS = ('2025-01-01', '2026-08-31 23:00')
FULL = ('2022-01-01', '2026-08-31 23:00')
LEVS = [1, 3, 5, 8, 10, 15, 20]
BTCETH = ['BTCUSDT', 'ETHUSDT']


def _run(args):
    kw = dict(args)
    per = kw.pop('period')
    kw['start'], kw['end'] = {'IS': IS, 'OOS': OOS, 'FULL': FULL}[per]
    r = cs.sim(**kw)
    r.pop('series', None)
    return r


def run_many(jobs):
    with Pool(4, initializer=cs.load) as p:
        return p.map(_run, jobs, chunksize=1)


def row(variant, L, params, rIS, rOOS, rFULL, extra=None):
    y = rFULL['years']
    d = dict(variant=variant, leverage=L, params=json.dumps(params),
             cagr_is=rIS['cagr'], cagr_oos=rOOS['cagr'], maxdd_is=rIS['maxdd'], maxdd_oos=rOOS['maxdd'],
             maxdd_full=rFULL['maxdd'], worst_day_is=rIS['worst_day'], worst_day_oos=rOOS['worst_day'],
             worst_day_full=rFULL['worst_day'], sharpe_is=rIS['sharpe'], sharpe_oos=rOOS['sharpe'],
             liq_is=rIS['liquidations'], liq_oos=rOOS['liquidations'], liq_full=rFULL['liquidations'],
             liq_dates_full=rFULL['liq_dates'], trades_full=rFULL['trades'], trades_oos=rOOS['trades'],
             imr_rejects_full=rFULL['imr_violations'], okx_openable=(rFULL['imr_violations'] == 0),
             avg_util_lev_full=rFULL['avg_lev'],
             funding_full=rFULL['funding'], interest_full=rFULL['interest'], fees_full=rFULL['fees'],
             **{f'ret_{yy}': y.get(yy, np.nan) for yy in range(2022, 2027)})
    if extra:
        d.update(extra)
    return d


if __name__ == '__main__':
    t0 = time.time()
    rows = []
    # ---------------- (i) always-on BTC+ETH: select rebalance band in-sample ----------------
    bands = [0.02, 0.05, 0.10, 0.20]
    jobs = [dict(coins=BTCETH, L=L, mode='always', band=b, period='IS') for L in LEVS for b in bands]
    res = run_many(jobs)
    sel_i = {}
    grid_rows = []
    for j, r in zip(jobs, res):
        grid_rows.append(dict(variant='always_BTCETH', L=j['L'], band=j['band'], cagr_is=r['cagr'], sharpe_is=r['sharpe'], maxdd_is=r['maxdd'], liq_is=r['liquidations']))
    g = pd.DataFrame(grid_rows)
    for L in LEVS:
        gg = g[g.L == L].sort_values('cagr_is', ascending=False)
        sel_i[L] = float(gg.iloc[0].band)
    print('selected bands (i):', sel_i, f'{time.time()-t0:.0f}s', flush=True)
    # main runs + sensitivities
    specs = []
    for L in LEVS:
        base = dict(coins=BTCETH, L=L, mode='always', band=sel_i[L])
        specs.append(('i_always_BTCETH', L, dict(base), {'borrow': 'okx', 'stress': 'mark', 'fees': 'taker'}))
        for b in [0.05, 0.08, 0.12]:
            specs.append((f'i_always_BTCETH_borrow{int(b*100)}', L, dict(base, borrow=b), {'borrow': b}))
        specs.append(('i_always_BTCETH_rawpremiumstress', L, dict(base, stress='raw'), {'stress': 'raw premium index'}))
        specs.append(('i_always_BTCETH_makerfees', L, dict(base, maker=True), {'fees': 'maker'}))
        specs.append(('i_always_BTCETH_splitmargin', L, dict(base, margin='split'), {'margin': 'separate accounts, daily transfer'}))
    jobs = []
    for s in specs:
        for per in ['IS', 'OOS', 'FULL']:
            jobs.append(dict(s[2], period=per))
    res = run_many(jobs)
    for n_, s in enumerate(specs):
        rIS, rOOS, rFULL = res[3 * n_: 3 * n_ + 3]
        p = {k: v for k, v in s[2].items() if k not in ('coins',)}
        p.update(s[3])
        rows.append(row(s[0], s[1], p, rIS, rOOS, rFULL))
    print('(i) done', f'{time.time()-t0:.0f}s', flush=True)

    # ---------------- (ii) dynamic rotation over 10 coins ----------------
    grid = list(itertools.product([3, 7, 14], [0.0, 0.05, 0.10, 0.20], [1, 2, 4], [0.05, 0.10]))
    jobs = [dict(coins=cs.ALL, L=L, mode='rotate', lookback_d=lb, theta=th, K=K, band=b, period='IS')
            for L in LEVS for (lb, th, K, b) in grid]
    res = run_many(jobs)
    for j, r in zip(jobs, res):
        grid_rows.append(dict(variant='rotate_10coins', L=j['L'], band=j['band'], lookback_d=j['lookback_d'], theta=j['theta'], K=j['K'],
                              cagr_is=r['cagr'], sharpe_is=r['sharpe'], maxdd_is=r['maxdd'], liq_is=r['liquidations']))
    g = pd.DataFrame(grid_rows)
    g.to_csv(f'{OUT}/is_grid.csv', index=False)
    sel_ii = {}
    for L in LEVS:
        gg = g[(g.variant == 'rotate_10coins') & (g.L == L)].sort_values('cagr_is', ascending=False)
        b = gg.iloc[0]
        sel_ii[L] = dict(lookback_d=int(b.lookback_d), theta=float(b.theta), K=int(b.K), band=float(b.band))
    print('selected (ii):', sel_ii, f'{time.time()-t0:.0f}s', flush=True)
    specs = []
    for L in LEVS:
        base = dict(coins=cs.ALL, L=L, mode='rotate', **sel_ii[L])
        specs.append(('ii_rotate_10coins', L, dict(base), {'borrow': 'okx'}))
        for bb in [0.05, 0.08, 0.12]:
            specs.append((f'ii_rotate_10coins_borrow{int(bb*100)}', L, dict(base, borrow=bb), {'borrow': bb}))
        specs.append(('ii_rotate_10coins_rawpremiumstress', L, dict(base, stress='raw'), {'stress': 'raw premium index'}))
    jobs = []
    for s in specs:
        for per in ['IS', 'OOS', 'FULL']:
            jobs.append(dict(s[2], period=per))
    res = run_many(jobs)
    for n_, s in enumerate(specs):
        rIS, rOOS, rFULL = res[3 * n_: 3 * n_ + 3]
        p = {k: v for k, v in s[2].items() if k not in ('coins',)}
        p.update(s[3])
        rows.append(row(s[0], s[1], p, rIS, rOOS, rFULL))
    df = pd.DataFrame(rows)
    df.to_csv(f'{OUT}/results.csv', index=False)
    json.dump(dict(selected_always=sel_i, selected_rotate=sel_ii, rows=rows), open(f'{OUT}/results.json', 'w'), indent=1, default=str)
    print('done', f'{time.time()-t0:.0f}s')
