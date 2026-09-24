"""Shared data loading, coin parameters, cost constants and portfolio metrics for the event/range family study.

Leverage definition (all three strategies): L = position notional at entry / equity of the sleeve that backs it.
Each coin is an isolated sleeve (isolated margin = the whole sleeve equity). The account holds 7 equal sleeves,
rebalanced to equal weight at every month start (sub-account transfers are free). Account gross notional / account
equity is therefore <= L (= L when every sleeve is in a position). A liquidation wipes that sleeve's margin (equity 0,
the remaining maintenance margin and the liquidation fee are treated as lost); the account continues with the other
sleeves and refills the dead sleeve at the next monthly rebalance.
"""
import json, os
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'AVAX', 'LINK']
LEVS = [1, 3, 5, 10, 15, 20]
IS0, IS1 = '2022-01-01', '2025-01-01'
OOS0, OOS1 = '2025-01-01', '2026-09-01'

# ---- costs, OKX VIP0 ----
FEE_T = 0.0005      # perp taker
FEE_M = 0.0002      # perp maker
RANGE_SLIP = 0.05   # extra taker slippage = 5% of the execution bar's high-low range (fast-market add-on)
CAP_FRAC = 0.75     # a stop may sit at most at 75% of the distance to the liquidation price

_meta = json.load(open(os.path.join(HERE, 'okx_meta.json')))
BASE_SLIP = {'BTC': 1e-4, 'ETH': 1e-4, 'SOL': 3e-4, 'XRP': 3e-4, 'DOGE': 5e-4, 'AVAX': 5e-4, 'LINK': 5e-4}
TICK = {c: _meta[c]['tickSz'] for c in COINS}              # OKX tick size
MMR = {c: _meta[c]['tiers'][0]['mmr'] for c in COINS}      # OKX tier-1 maintenance margin rate (cross), fetched 2026-09-24


def load(coin):
    z = np.load(os.path.join(DATA, f'{coin}_1m.npz'))
    d = {k: z[k] for k in z.files}
    for k in ('qv', 'tbq'):
        d[k] = d[k].astype(np.float64)
    t = d['t']
    d['day'] = (t // 86_400_000).astype(np.int64)
    ts = pd.to_datetime(t, unit='ms')
    d['month'] = (ts.year * 12 + ts.month - 1).values.astype(np.int64)
    return d


def window(d, a, b):
    ta = pd.Timestamp(a).value // 10**6; tb = pd.Timestamp(b).value // 10**6
    return int(np.searchsorted(d['t'], ta)), int(np.searchsorted(d['t'], tb))


# ---------------- portfolio aggregation and metrics ----------------
def combine(sleeves, day0, months):
    """sleeves: list of (r, tr) daily arrays (same length). r = close/ref - 1, tr = trough/ref (ref = previous close,
    or 1 after a revival). Monthly equal-weight rebalance. Returns daily portfolio close value and trough value."""
    R = np.stack([s[0] for s in sleeves]); T = np.stack([s[1] for s in sleeves])
    K, nd = R.shape
    V = np.empty(nd); TR = np.empty(nd)
    v0 = 1.0
    grow = np.ones(K)
    for d in range(nd):
        if d == 0 or months[d] != months[d - 1]:
            if d > 0:
                v0 = V[d - 1]
            grow[:] = 1.0
        prev = grow.copy()
        grow *= (1.0 + R[:, d])
        V[d] = v0 * grow.mean()
        TR[d] = v0 * (prev * T[:, d]).mean()
    return V, TR


def metrics(V, TR, dates):
    """V, TR: daily portfolio close value and intraday trough value (start value 1 before the first day)."""
    nd = len(V)
    prevV = np.concatenate([[1.0], V[:-1]])
    with np.errstate(divide='ignore', invalid='ignore'):
        dr = np.where(prevV > 0, V / prevV - 1.0, 0.0)
        dtr = np.where(prevV > 0, TR / prevV - 1.0, 0.0)
    years = nd / 365.25
    cagr = (V[-1] ** (1 / years) - 1) if V[-1] > 0 else -1.0
    run = np.maximum.accumulate(np.concatenate([[1.0], V]))     # run[d] = peak of closes before day d (incl. start 1)
    dd_trough = 1 - TR / run[:-1]                                   # intraday trough vs prior peak
    dd_close = 1 - V / run[1:]
    dd = float(max(dd_trough.max(), dd_close.max()))
    sd = dr.std()
    sharpe = dr.mean() / sd * np.sqrt(365) if sd > 0 else 0.0
    yrs = pd.DatetimeIndex(dates).year
    per_year = {}
    for y in np.unique(yrs):
        m = yrs == y
        first = np.argmax(m); last = len(m) - 1 - np.argmax(m[::-1])
        start = prevV[first]
        per_year[int(y)] = (V[last] / start - 1) if start > 0 else 0.0
    return dict(cagr=float(cagr), maxdd=float(min(dd, 1.0)), worst_day=float(dr.min()), worst_intraday=float(dtr.min()),
                sharpe=float(sharpe), final=float(V[-1]), per_year=per_year)
