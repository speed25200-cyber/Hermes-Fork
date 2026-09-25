"""Verification helpers (read-only use of the author's data)."""
import sys, types
import numpy as np, pandas as pd
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve')
sys.path.insert(0, SP + '/newlisting')
import sim
from livesim import simulate_live, sharpe
SIG = 0.1238230231575359
H = 240
RULE = dict(tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True)


def load_okx(path=SP + '/xlist/data/sim'):
    old = sim.D
    sim.D = path
    try:
        d = sim.Data('binance')
    finally:
        sim.D = old
    d.sigma_ref = SIG
    return d


def load_bn():
    old = sim.D
    sim.D = SP + '/newlisting/data'
    try:
        d = sim.Data('hybrid')
    finally:
        sim.D = old
    d.sigma_ref = SIG
    return d


def merge(a, b, keep_a=None, keep_b=None, newtok_b=None, newtok_a=None):
    m = types.SimpleNamespace()
    ka = np.ones(a.n, bool) if keep_a is None else keep_a
    kb = np.ones(b.n, bool) if keep_b is None else keep_b
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka, :H], getattr(b, k)[kb, :H]])
    for k in ('vol_d', 'vol_n'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka, :H - 1], getattr(b, k)[kb, :H - 1]])
    for k in ('g0', 'year'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka], getattr(b, k)[kb]])
    na = a.newtok if newtok_a is None else newtok_a
    nb = b.newtok if newtok_b is None else newtok_b
    m.newtok = np.concatenate([na[ka], nb[kb]])
    ea = a.ev[['sym', 't0']].assign(src='binance', j=np.arange(a.n))[ka]
    eb = b.ev[['sym', 't0']].assign(src='okx', j=np.arange(b.n))[kb]
    m.ev = pd.concat([ea, eb], ignore_index=True)
    m.n, m.H = len(m.ev), H
    m.sigma_ref = SIG
    return m


def run(D, start, end, **kw):
    r = simulate_live(D, start=start, end=end, **{**RULE, **kw})
    tr = pd.DataFrame(r['trades'])
    return r, tr


def tstat(x):
    x = np.asarray(x, float)
    return float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))) if len(x) > 2 else float('nan')
