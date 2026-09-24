"""Event lists for the wick/cascade reversal and session tables for the opening-range breakout (all causal)."""
import numpy as np, pandas as pd

ROLL = 4320   # 3 days of 1m bars for the volatility / volume baselines


def wick_events(d, w, z, V):
    """Event at the CLOSE of bar i if |ln(c_i/c_{i-w})| >= z*sqrt(w)*sigma_1m and window quote volume >= V x baseline.
    sigma_1m = std of 1m log returns over the 3 days ending at bar i-w (move window excluded); baseline = mean 1m quote
    volume over the same 3 days. No missing (filled) bar allowed in the window. Direction = fade (-sign of the move).
    mf = |c_i - c_{i-w}| / c_i (move size as a fraction of price)."""
    c = d['c']
    r1 = np.zeros_like(c); r1[1:] = np.diff(np.log(c))
    s1 = pd.Series(r1).rolling(ROLL, min_periods=ROLL // 2).std().shift(w).values
    qv = pd.Series(d['qv'])
    base = qv.rolling(ROLL, min_periods=ROLL // 2).mean().shift(w).values
    vw = qv.rolling(w).sum().values
    miss = pd.Series(d['miss'].astype(float)).rolling(w + 1).max().values
    rw = np.full_like(c, np.nan); rw[w:] = np.log(c[w:] / c[:-w])
    with np.errstate(invalid='ignore', divide='ignore'):
        ok = (np.abs(rw) >= z * np.sqrt(w) * s1) & (vw >= V * w * base) & (base > 0) & (miss == 0)
    idx = np.nonzero(ok)[0].astype(np.int64)
    dr = (-np.sign(rw[idx])).astype(np.int64)
    mf = np.abs(c[idx] - c[idx - w]) / c[idx]
    return idx, dr, mf


def sessions(d, which, M):
    """Session opens: asia = 00:00 UTC every day; eu = 08:00 Europe/London Mon-Fri; us = 09:30 America/New_York Mon-Fri.
    Opening range = bars [s0, s0+M). Entry window [s0+M, s0+240), forced exit at s0+480.
    Returns arm, until, exit, orh, orl, width_frac, clean (no filled bar inside the range)."""
    t = d['t']; t0 = int(t[0]); n = len(t)
    days = pd.date_range(pd.to_datetime(t0, unit='ms').normalize(), pd.to_datetime(int(t[-1]), unit='ms').normalize(), freq='D')
    if which == 'asia':
        opens = days
    elif which == 'eu':
        opens = pd.DatetimeIndex([pd.Timestamp(x.date()).tz_localize('Europe/London') + pd.Timedelta(hours=8) for x in days if x.weekday() < 5]).tz_convert('UTC').tz_localize(None)
    elif which == 'us':
        opens = pd.DatetimeIndex([pd.Timestamp(x.date()).tz_localize('America/New_York') + pd.Timedelta(hours=9, minutes=30) for x in days if x.weekday() < 5]).tz_convert('UTC').tz_localize(None)
    s0 = ((opens.values.astype('datetime64[ms]').astype(np.int64) - t0) // 60000).astype(np.int64)
    s0 = s0[(s0 >= 0) & (s0 + 480 < n)]
    h, l, c, miss = d['h'], d['l'], d['c'], d['miss']
    orh = np.array([h[a:a + M].max() for a in s0]); orl = np.array([l[a:a + M].min() for a in s0])
    clean = np.array([miss[a:a + M].max() == 0 for a in s0])
    width = (orh - orl) / c[s0 + M - 1]
    return s0 + M, s0 + 240, s0 + 480, orh, orl, width, clean


def narrow_flag(width):
    """1 if this session's range width is below the median of the previous 20 sessions (same type, same M)."""
    med = pd.Series(width).rolling(20, min_periods=20).median().shift(1).values
    with np.errstate(invalid='ignore'):
        return (width < med).astype(np.int64)
