"""Frozen live rule (review_sleeve/livesim.simulate_live: tranches +24 h and +72 h, 3-hour entry window, exit +168 h,
shared stop +50 %, BTC hedge 1:1, 5 slots, vol scaling with the research sigma_ref, research costs) on the OKX-listing
events, which were never used to choose the rule. -> results/okx_events.json, results/okx_trades.csv"""
import sys, json
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve')
sys.path.insert(0, SP + '/newlisting')
import sim
suffix = '_all' if '--all' in sys.argv else ''
sim.D = f'{SP}/xlist/data/sim{suffix}'
from livesim import simulate_live, sharpe
import numpy as np, pandas as pd, os

Dt = sim.Data('binance')
Dt.sigma_ref = 0.1238230231575359                   # research value (IS Binance events), as in the live config
END = '2026-09-25'
out = {}


def tstat(x):
    x = np.asarray(x, float)
    return float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))) if len(x) > 2 else float('nan')


def run(name, start, end, **kw):
    r = simulate_live(Dt, tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True,
                      start=start, end=end, **kw)
    tr = pd.DataFrame(r['trades'])
    ret = r['ret']
    yrs = {int(k): round(float(np.prod(1 + v) - 1), 4) for k, v in ret.groupby(ret.index.year)}
    rec = dict(sharpe=round(sharpe(ret), 3), n_trades=len(tr), mean_trade=round(float(tr.ret.mean()), 4) if len(tr) else None,
               median_trade=round(float(tr.ret.median()), 4) if len(tr) else None, t_trades=round(tstat(tr.ret), 2) if len(tr) else None,
               win=round(float((tr.ret > 0).mean()), 3) if len(tr) else None, years=yrs, stats=r['stats'],
               maxdd=round(float(r.get('maxdd', np.nan)), 4) if 'maxdd' in r else None)
    out[name] = rec
    print(f"{name:34s} Sharpe {rec['sharpe']:.2f} trades {rec['n_trades']} mean {rec['mean_trade']} med {rec['median_trade']} t {rec['t_trades']} win {rec['win']} {yrs}", flush=True)
    return r, tr


r, tr = run('all 2022-2026', '2022-01-01', END)
run('2022-2024', '2022-01-01', '2025-01-01')
run('2025-2026', '2025-01-01', END)
run('2026', '2026-01-01', END)
run('all, costs x2', '2022-01-01', END, cost_mult=2.0)
tr['sym'] = tr['sym'] if 'sym' in tr else Dt.ev.sym.values[tr.i]
tr = tr.merge(Dt.ev[['sym', 'cls']], on='sym', how='left')
tr.to_csv(f'{SP}/xlist/results/okx_trades{suffix}.csv', index=False)
for c, g in tr.groupby('cls'):
    print(f'  class {c:10s} trades {len(g)} mean {g.ret.mean():.4f} t {tstat(g.ret):.2f} win {(g.ret > 0).mean():.2f}')
json.dump(out, open(f'{SP}/xlist/results/okx_events{suffix}.json', 'w'), indent=1, default=str)
