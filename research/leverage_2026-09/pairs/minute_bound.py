"""1-minute conservative intrabar bound for a pair position (liquidation / intrabar drawdown).

For a pair (A, B) with notional ratio r = |notional B| / |notional A| and each 1h bar i (open prices Oa, Ob), using
Binance 1m MARK-price klines of both legs:
   long spread  (long A, short B): WL_i(r) = min over minutes m in bar i of [ (lowA_m/Oa - 1) - r * (highB_m/Ob - 1) ]
   short spread (short A, long B): WS_i(r) = min over minutes m of [ -(highA_m/Oa - 1) + r * (lowB_m/Ob - 1) ]
Within one minute the long leg is put at its low and the short leg at its high simultaneously, so the true unrealized
P&L at every instant is >= UPL(open) + notional_A * W (a rigorous lower bound, just 60x finer than the hourly one).
W is concave in r, so min(W(0.8*beta), W(1.25*beta)) bounds every ratio in [0.8, 1.25]*beta; the simulator falls
back to the hourly bound when the live ratio leaves that band or a minute series is missing.
Everything is computed in memory (1m files are not written to disk).
"""
import io, zipfile, os
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
from dl_daily import get

R = 'https://data.binance.vision/data/futures/um/monthly/markPriceKlines'


def fetch_1m(sym, month):
    b = get(f'{R}/{sym}/1m/{sym}-1m-{month}.zip')
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=0 if not raw[:1].isdigit() else None, usecols=[0, 2, 3])
    df.columns = ['t', 'h', 'l']
    df = df[pd.to_numeric(df.t, errors='coerce').notna()].astype(float)
    return df


def month_minutes(sym, month, m0, nmin):
    """Return (high, low) float64 arrays on the month's minute grid (NaN where missing)."""
    df = fetch_1m(sym, month)
    H = np.full(nmin, np.nan); L = np.full(nmin, np.nan)
    if df is None or not len(df):
        return H, L
    idx = ((df.t.values - m0) // 60000).astype(np.int64)
    ok = (idx >= 0) & (idx < nmin)
    H[idx[ok]] = df.h.values[ok]; L[idx[ok]] = df.l.values[ok]
    return H, L


def compute_bounds(sel_all, syms, O, GRID, form_dates, log=print):
    """sel_all: iterable of (form_date Timestamp, a, b, beta). Returns dict key -> (WL, WS) arrays over the month's bars."""
    by_month = {}
    for t, a, b, be in sel_all:
        by_month.setdefault(pd.Timestamp(t), set()).add((a, b, float(be)))
    out = {}
    fl = list(form_dates)
    for mi, t in enumerate(fl):
        if t not in by_month:
            continue
        t1 = fl[mi + 1] if mi + 1 < len(fl) else t + pd.offsets.MonthBegin(1)
        i0 = GRID.get_loc(t); nh = int((t1 - t) / pd.Timedelta(hours=1))
        nmin = nh * 60
        m0 = t.value // 10**6
        need = sorted({x for p in by_month[t] for x in p[:2]})
        month = t.strftime('%Y-%m')
        with ThreadPoolExecutor(16) as ex:
            res = dict(zip(need, ex.map(lambda s: month_minutes(syms[s], month, m0, nmin), need)))
        for (a, b, be) in by_month[t]:
            Ha, La = res[a]; Hb, Lb = res[b]
            Oa = np.repeat(O[a, i0:i0 + nh], 60); Ob = np.repeat(O[b, i0:i0 + nh], 60)
            xal = La / Oa - 1; xah = Ha / Oa - 1; xbl = Lb / Ob - 1; xbh = Hb / Ob - 1
            W = []
            for r in (0.8 * be, 1.25 * be):
                wl = (xal - r * xbh).reshape(nh, 60); ws = (-xah + r * xbl).reshape(nh, 60)
                # min over the 60 minutes; NaN (-> hourly fallback) if any minute of either leg is missing
                W.append((wl.min(1), ws.min(1)))
            WL = np.minimum(W[0][0], W[1][0]); WS = np.minimum(W[0][1], W[1][1])
            out[(t, a, b, round(be, 10))] = (WL.astype(np.float64), WS.astype(np.float64))
        log(f'1m bounds {month}: {len(need)} symbols, {len(by_month[t])} pairs')
    return out
