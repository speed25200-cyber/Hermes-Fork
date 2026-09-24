"""Evaluate IS-selected configs at leverages 1..20 under each margin mode; IS, OOS and FULL periods.
Configs were fixed from grid_is.csv / grid_is_lev.csv (2022-2024 only)."""
import pickle, json, sys, numpy as np, pandas as pd, multiprocessing as mp, cb_backtest as cb
AS = cb.load_all()
PER = {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}
CFG = {
    'short_A': dict(signal='net', h_in=0.08, h_out=0.02, tau_min=30),                                   # family spec, IS-best Sharpe @1x
    'short_B': dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30),                                  # family spec, IS-best CAGR @10-20x
    'two_A': dict(signal='net', h_in=0.08, h_out=0.02, tau_min=30, h_in_rev=-0.02, h_out_rev=0.02),    # both directions, IS-best Sharpe @1x
    'two_B': dict(signal='net', h_in=0.04, h_out=0.0, tau_min=30, h_in_rev=-0.02, h_out_rev=0.02),     # both directions, IS-best CAGR @3-20x
    'long_A': dict(signal='raw', h_in=None, h_out=None, h_in_rev=0.08, h_out_rev=None, tau_min=30),     # reverse only, IS-best Sharpe @1x
    'long_B': dict(signal='net', h_in=None, h_out=None, h_in_rev=-0.02, h_out_rev=0.02, tau_min=30),    # reverse only, IS-best CAGR
}
LEVS = [1, 3, 5, 10, 15, 20]
MODES = ['cross', 'cross_stress', 'pm_approx', 'isolated', 'isolated_rb']
def row(cfg, scope, mode, L, per, m, cost_tag):
    r = dict(config=cfg, cost=cost_tag, scope=scope, mode=mode, L=L, period=per)
    r.update({k: v for k, v in m.items() if k != 'years'})
    for y, v in m['years'].items():
        r['y' + y] = v
    return r
def job(arg):
    cfg, mode, L, per, cost_tag, cost = arg
    a, b = PER[per]
    sims, m, ec = cb.run_assets([AS['BTC'], AS['ETH']], CFG[cfg], L, mode, a, b, cost=cost)
    out = [row(cfg, 'BTC+ETH', mode, L, per, m, cost_tag)]
    for s, nm in zip(sims, ['BTC', 'ETH']):
        e1, l1 = cb.equity_series([s], [1.0], a, b)
        mm = cb.metrics_from(e1, l1); mm.update(nliq=s.nliq, nentry=s.nentry, nroll=s.nroll, nrb=s.nrb)
        out.append(row(cfg, nm, mode, L, per, mm, cost_tag))
    days_in = 0.0
    for s in sims:
        for t in s.trades:
            days_in += (pd.Timestamp(t['exit']) - pd.Timestamp(t['entry'])).total_seconds() / 86400
    out[0]['days_in_pos_avg'] = days_in / 2
    return out
COSTS = {
    'base': cb.COST,
    'maker_fut': dict(cb.COST),   # handled via p['maker_fut'] flag below
    'mmr1pct': dict(cb.COST, mmr_perp=0.01, mmr_fut=0.01),
    'slip_x2': dict(cb.COST, slip_perp=0.0002, slip_fut=0.0006),
    'deliv_0.02': dict(cb.COST, deliv_fee=0.0002),
}
if __name__ == '__main__':
    args = [(c, m, L, per, 'base', cb.COST) for c in CFG for m in MODES for L in LEVS for per in PER]
    for c in ['short_A', 'short_B', 'two_A', 'two_B']:
        for tag in ['mmr1pct', 'slip_x2', 'deliv_0.02']:
            for L in LEVS:
                for per in PER:
                    args.append((c, 'cross', L, per, tag, COSTS[tag]))
    # maker-on-futures-leg variant as separate configs
    for c in ['short_A', 'short_B', 'two_A', 'two_B']:
        CFG[c + '_mk'] = dict(CFG[c], maker_fut=True)
        for L in LEVS:
            for per in PER:
                args.append((c + '_mk', 'cross', L, per, 'maker_fut', cb.COST))
    print(len(args), 'runs', flush=True)
    with mp.Pool(4) as pool:
        res = pool.map(job, args, chunksize=4)
    df = pd.DataFrame([r for rr in res for r in rr])
    df.to_csv('results_all.csv', index=False)
    print('saved', len(df))
