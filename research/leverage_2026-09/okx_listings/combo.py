"""Union sleeve: Binance-listing events (research data, hybrid OKX/Binance prices) + OKX-first/OKX-only events, one
portfolio under the frozen live rule, against the Binance-only sleeve. -> results/union.json"""
import sys, json, types
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve')
sys.path.insert(0, SP + '/newlisting')
import sim
import numpy as np, pandas as pd
from livesim import simulate_live, sharpe
H = 240
Db = sim.Data('hybrid')
sim.D = f'{SP}/xlist/data/sim'
Do = sim.Data('binance')


def merge(a, b, keep_b=None):
    m = types.SimpleNamespace()
    kb = np.ones(b.n, bool) if keep_b is None else keep_b
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[:, :H], getattr(b, k)[kb, :H]])
    for k in ('vol_d', 'vol_n'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[:, :H - 1], getattr(b, k)[kb, :H - 1]])
    for k in ('g0', 'newtok', 'year'):
        m.__dict__[k] = np.concatenate([getattr(a, k), getattr(b, k)[kb]])
    ea = a.ev[['sym', 't0']].assign(src='binance')
    eb = b.ev[['sym', 't0']].assign(src='okx')[kb]
    m.ev = pd.concat([ea, eb], ignore_index=True)
    m.n, m.H = len(m.ev), H
    m.sigma_ref = 0.1238230231575359
    return m


Db.sigma_ref = Do.sigma_ref = 0.1238230231575359
# Binance-only, truncated to the same horizon (sanity: must match the full-horizon run)
Db_t = merge(Db, Do, keep_b=np.zeros(Do.n, bool))
U = merge(Db, Do)
periods = [('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-01'),
           ('2026 Jan-Aug', '2026-01-01', '2026-09-01'), ('2022-2026', '2022-01-01', '2026-09-01')]
out = {}
for name, D_ in (('binance_only', Db_t), ('union', U)):
    for pn, a, b in periods:
        r = simulate_live(D_, tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True, start=a, end=b)
        ret = r['ret']
        tr = pd.DataFrame(r['trades'])
        src = D_.ev.src.values[tr.i] if len(tr) else []
        m = ret.index.to_period('M')
        lomo = min(sharpe(ret[m != p]) for p in m.unique())
        yrs = {int(k): round(float(np.prod(1 + v) - 1), 3) for k, v in ret.groupby(ret.index.year)}
        rec = dict(sharpe=round(sharpe(ret), 3), lomo=round(lomo, 3), trades=len(tr), okx_trades=int((np.asarray(src) == 'okx').sum()),
                   maxdd=round(r['maxdd'], 3), vol=round(float(ret.std() * np.sqrt(365)), 3), years=yrs,
                   invested=round(float((ret != 0).mean()), 3))
        out[f'{name} {pn}'] = rec
        print(f'{name:13s} {pn:13s} Sharpe {rec["sharpe"]:.2f} LOMO {rec["lomo"]:.2f} trades {rec["trades"]} (okx {rec["okx_trades"]}) maxDD {rec["maxdd"]:.3f} vol {rec["vol"]:.3f} {yrs}', flush=True)
json.dump(out, open(f'{SP}/xlist/results/union.json', 'w'), indent=1)
