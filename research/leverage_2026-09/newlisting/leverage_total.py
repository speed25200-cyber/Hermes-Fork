"""Leverage table in the exchange convention (L = total gross notional of all legs / equity at entry, BTC hedge
included): coin-leg cap = L / (1 + beta). OOS intrabar max DD + liquidation, and half-Kelly (IS daily returns) in the
same units, for the IS-selected config under both price sources. -> results/leverage_total.json"""
import json
from sim import *
out = {}
for price in ('hybrid', 'binance'):
    Dt = Data(price)
    cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
    base = 1 + cfg['beta']
    ri = simulate(Dt, cfg, IS_START, OOS_START, L=1 / base)['ret']      # total gross cap 1x
    hk = 0.5 * ri.mean() / ri.var()
    tab = {}
    for L in [1, 2, 3, 5, 8, 10, 15, 20]:
        r = simulate(Dt, cfg, OOS_START, OOS_END, L=L / base)
        s = summarize(r)
        tab[L] = dict(coin_leg_cap=L / base, oos_sharpe=s['sharpe'], oos_cagr=s['cagr'], oos_maxdd=s['maxdd'], liq=s['liq'],
                      y2025=s['years'].get(2025), y2026=s['years'].get(2026))
    ok = [L for L, v in tab.items() if v['oos_maxdd'] <= 0.35 and not v['liq'] and L <= hk]
    out[price] = dict(half_kelly_total=hk, table=tab, supportable_L_total=max(ok) if ok else 0)
    print(price, 'half-Kelly', round(hk, 2), {L: (round(v['oos_maxdd'], 3), v['liq'], round(v['oos_cagr'], 3)) for L, v in tab.items()}, 'supportable', out[price]['supportable_L_total'])
json.dump(out, open(os.path.join(BASE, 'results', 'leverage_total.json'), 'w'), indent=1)
