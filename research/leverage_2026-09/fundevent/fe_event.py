"""Families (i) settlement capture and (ii) settlement drift: per-event trade P&L on 1m bars + portfolio at leverage L.

Event = (symbol, settlement time t) with a signal known at decision time:
  sig='lag'    : annualised rate of the PREVIOUS settlement of that symbol (published before t)  -> tradable
  sig='oracle' : annualised rate realised AT t (not known before t)                            -> upper bound only
Trade spec (a, b, dir): enter at the open of the 1m bar starting t+a min, exit at the open of the bar starting t+b.
  dir='recv' : side = -sign(signal) (the side that RECEIVES the funding); 'pay' : the opposite side.
  Funding is exchanged only if a < 0 < b (position open at t).  (i) = straddling specs, (ii) = specs entirely
  before (b = -1) or after (a = +1) the settlement (no funding).
Hedge: 'btc' = opposite BTCUSDT perp position of beta x coin notional (beta = 7d OLS on 1h returns, known at t-1h);
       'none' = coin leg alone.
Execution:
  taker: coin taker 0.05% + slippage (tier by Binance 24h quote volume 3/5/8/15 bp + 10% of the 1m bar range),
         BTC taker 0.05% + 1bp + 10% of the BTC 1m bar range, both at entry and exit.
  maker: coin entry = limit at the open of bar a; fills only if a later bar (before exit and before t for
         straddling specs) trades THROUGH it by >= 1 tick (buy: low <= limit - tick); maker fee 0.02%, no slippage;
         unfilled -> no trade (opportunity cost). BTC hedge taker at the close of the fill bar.
         Coin exit = limit at the open of bar b, same trade-through rule within the next W = min(15, window) bars;
         else taker at the open of bar b+W with slippage.  Tick = smallest positive price increment seen in the
         122-bar window (>= true tick, i.e. conservative).
Risk (per event pair, own cross-margin sub-account with margin m, coin notional L*m):
  worst equity in each held 1m bar = coin at its ADVERSE extreme and BTC at its adverse extreme simultaneously;
  maintenance margin = OKX tier-1 MMR of the coin (default 2.5% if not on OKX) + 0.4% x BTC hedge notional;
  liquidation when worst equity <= MM  -> the pair's whole margin is lost (margin + fee), others continue.
  Eligibility: OKX initial margin L*(1/maxLev_coin + beta/100) <= 1 (else the position cannot be opened).
Portfolio: at each settlement time T, the events passing |signal| >= thr, ranked by |signal|, top K; equity split
  equally (1/N each, N <= K); the pair returns compound. Leverage L = coin-leg notional / account equity at entry
  (the BTC hedge adds beta*L of notional on top).
"""
import os, sys, json, itertools, time
import numpy as np, pandas as pd
from common import D

PRE, NB = 61, 122
FEE_T, FEE_M = 5e-4, 2e-4
FEE_SPOT = 1e-3
BTC_SLIP0, BTC_MMR = 1e-4, 0.004
RANGE_K = 0.10
IS0, IS1, OOS1 = pd.Timestamp('2022-01-01'), pd.Timestamp('2025-01-01'), pd.Timestamp('2026-09-01')
LEVS = [1, 3, 5, 10, 15, 20]


def load():
    ev = pd.read_parquet(os.path.join(D, 'ev.parquet'))
    pp = os.path.join(D, 'pred.npz')
    if os.path.exists(pp):
        z = np.load(pp)
        # Binance scales the 8h-formula rate for shorter intervals (x N/8), and the practice changed over time
        # (4h contracts: x1 in 2022-24, x0.5 later).  Causal calibration: for each (interval, calendar month) the
        # factor is the median realised/predicted ratio of the PREVIOUS month (events with |pred| >= 100%/yr),
        # snapped to {1/8, 1/4, 1/2, 1}; carried forward when a month has < 50 such events.
        p5 = z['pred_k5']
        mon = pd.to_datetime(ev.t.values, unit='ms').to_period('M')
        mi = (mon.year - 2022) * 12 + mon.month - 1
        f = np.ones(len(ev))
        fac_log = {}
        for I in np.unique(ev.interval_h.values):
            mI = ev.interval_h.values == I
            big = mI & np.isfinite(p5) & (np.abs(p5 * 8760 / I) >= 1.0)
            cur = 1.0
            for m_ in range(int(mi.min()), int(mi.max()) + 1):
                prev = big & (mi == m_ - 1)
                if prev.sum() >= 50:
                    raw = np.median(ev.rate.values[prev] / p5[prev])
                    cur = min([0.125, 0.25, 0.5, 1.0], key=lambda c: abs(np.log(c / max(raw, 1e-6))))
                f[mI & (mi == m_)] = cur
                fac_log[(float(I), m_)] = cur
        for kx in z.files:
            ev[kx] = z[kx] * f
        chg = {}
        for I in sorted({k[0] for k in fac_log}):
            seq = [(m_, v) for (i_, m_), v in sorted(fac_log.items()) if i_ == I]
            chg[I] = [(2022 + m_ // 12, m_ % 12 + 1, v) for j, (m_, v) in enumerate(seq) if j == 0 or v != seq[j - 1][1]]
        print('predicted-funding factor changes (year, month, factor):', chg, flush=True)
    W = np.load(os.path.join(D, 'win_perp.npy'), mmap_mode='r')
    WB = np.load(os.path.join(D, 'win_btc.npy'), mmap_mode='r')
    return ev, W, WB


def signal(ev, sig, a):
    """Annualised decision-time signal for a trade entered at t+a minutes.
    lag    : previous settlement's realised rate (published at t_prev)
    pred   : a < 0 -> Binance-style predicted rate from premium-index minutes closed by t+a (dl_premium.py);
             a > 0 -> the rate realised at t (published at t, known when entering after the settlement)
    oracle : rate realised at t, used even for entries before t (NOT tradable, upper bound)"""
    if sig == 'lag':
        return ev.ann_prev.values
    if sig == 'oracle' or (sig == 'pred' and a > 0):
        return ev.ann.values
    k = -a
    return ev[f'pred_k{k}'].values * 8760.0 / ev.interval_h.values


def tick_rel(Wc):
    """smallest positive increment among o,h,l,c of the window / median price."""
    x = Wc[:, :, :4].reshape(len(Wc), -1).astype(np.float64)
    x = np.sort(x, axis=1)
    d = np.diff(x, axis=1)
    d[~(d > 0)] = np.inf
    tk = d.min(axis=1)
    med = np.nanmedian(Wc[:, :, 3], axis=1)
    return tk / med


def trades(ev, W, WB, a, b, direction, sig, hedge, execm, chunk=20000):
    """Per-event results for one spec. Returns dict of arrays (len = len(ev))."""
    n = len(ev)
    out = {k: np.full(n, np.nan) for k in ['r', 'z', 'trough', 'fund', 'px', 'hdg', 'cost']}
    out['traded'] = np.zeros(n, bool)
    s_all = np.nan_to_num(np.sign(signal(ev, sig, a)))
    if direction == 'recv':
        s_all = -s_all
    straddle = (a < 0 < b)
    ja, jb = a + PRE, b + PRE
    for c0 in range(0, n, chunk):
        c1 = min(n, c0 + chunk)
        sl = slice(c0, c1)
        Wc = np.asarray(W[sl], dtype=np.float64)
        Bc = np.asarray(WB[sl], dtype=np.float64)
        O, H, L, C = Wc[:, :, 0], Wc[:, :, 1], Wc[:, :, 2], Wc[:, :, 3]
        BO, BH, BL, BC = Bc[:, :, 0], Bc[:, :, 1], Bc[:, :, 2], Bc[:, :, 3]
        s = s_all[sl]
        m = len(s)
        beta = ev.beta.values[sl] if hedge == 'btc' else (np.ones(m) if hedge == 'spot' else np.zeros(m))
        # hedge-leg execution constants: BTC perp (taker 0.05% + 1bp) or the coin's spot (OKX spot taker 0.10%
        # + the coin's slippage tier); spot hedge = long spot against a short perp, no MM, no funding
        HFEE = FEE_SPOT if hedge == 'spot' else FEE_T
        HSLIP = ev.slip0.values[sl] if hedge == 'spot' else BTC_SLIP0
        HMMR = 0.0 if hedge == 'spot' else BTC_MMR
        slip0 = ev.slip0.values[sl]
        mmr = ev.mmr.values[sl]
        rate = ev.rate.values[sl]
        rbtc = ev.rate_btc.values[sl]
        rows = np.arange(m)
        rngc = (H - L) / O
        rngb = (BH - BL) / BO
        J = np.arange(NB)[None, :]
        if execm == 'taker':
            je = np.full(m, ja); jx = np.full(m, jb)
            Pe = O[:, ja]; Px = O[:, jb]
            Be = BO[:, ja]; Bx = BO[:, jb]
            cin = FEE_T + slip0 + RANGE_K * rngc[:, ja] + beta * (HFEE + HSLIP + RANGE_K * rngb[:, ja])
            cout = FEE_T + slip0 + RANGE_K * rngc[:, jb] + beta * (HFEE + HSLIP + RANGE_K * rngb[:, jb])
            filled = np.isfinite(Pe) & np.isfinite(Px)
            start = je; end = jx          # held bars: [start, end)
        else:
            tk = tick_rel(Wc) * np.nanmedian(C, axis=1)
            Pe = O[:, ja]
            last_fill = (min(jb, PRE) if straddle else jb) - 1        # fill bar must end before t / before exit
            cand = (J >= ja) & (J <= last_fill)
            thr_hit = np.where(s[:, None] > 0, L <= (Pe - tk)[:, None], H >= (Pe + tk)[:, None]) & cand
            anyf = thr_hit.any(axis=1)
            jf = np.where(anyf, thr_hit.argmax(axis=1), -1)
            jfc = np.clip(jf, 0, NB - 1)
            Be = BC[rows, jfc]                                        # BTC hedge at the close of the fill bar
            jnext = np.clip(jfc + 1, 0, NB - 1)
            cin = FEE_M + beta * (HFEE + HSLIP + RANGE_K * rngb[rows, jnext])
            # exit
            Wn = int(min(15, NB - 1 - jb))
            Pxl = O[:, jb]
            cand2 = (J >= jb) & (J < jb + Wn)
            hit2 = np.where(s[:, None] > 0, H >= (Pxl + tk)[:, None], L <= (Pxl - tk)[:, None]) & cand2
            any2 = hit2.any(axis=1)
            jx2 = np.where(any2, hit2.argmax(axis=1), jb + Wn)
            Px = np.where(any2, Pxl, O[:, jb + Wn])
            Bx = np.where(any2, BC[rows, jx2], BO[:, jb + Wn])
            jnx = np.clip(np.where(any2, jx2 + 1, jb + Wn), 0, NB - 1)
            cout = np.where(any2, FEE_M, FEE_T + slip0 + RANGE_K * rngc[:, jb + Wn]) + \
                beta * (HFEE + HSLIP + RANGE_K * rngb[rows, jnx])
            filled = anyf & np.isfinite(Pe) & np.isfinite(Px)
            start = jfc
            end = np.where(any2, jx2 + 1, jb + Wn)
        coin = s * (Px / Pe - 1.0)
        if straddle:
            fund = -s * rate * (O[:, PRE] / Pe)
            hf = s * beta * rbtc * (BO[:, PRE] / Be) if hedge == 'btc' else np.zeros(m)
        else:
            fund = np.zeros(m); hf = np.zeros(m)
        hdg = np.where(beta > 0, -s * beta * (Bx / Be - 1.0) + hf, 0.0)
        hf = np.where(beta > 0, hf, 0.0)
        r = coin + fund + hdg - cin - cout
        # path
        held = (J >= start[:, None]) & (J < end[:, None])
        cadv = np.where(s[:, None] > 0, L, H)
        badv = np.where(s[:, None] > 0, BH, BL)
        cw = s[:, None] * (cadv / Pe[:, None] - 1.0)
        if hedge in ('btc', 'spot'):
            hw = -s[:, None] * beta[:, None] * (badv / Be[:, None] - 1.0)
            mmb = beta[:, None] * HMMR * badv / Be[:, None]
        else:
            hw = 0.0; mmb = 0.0
        fw = (fund + hf)[:, None] * (J >= PRE) if straddle else 0.0
        worst = cw + hw + fw
        mm = mmr[:, None] * cadv / Pe[:, None] + mmb
        # bars with missing data (exchange gaps) inside the holding window are skipped
        wz = np.where(held & np.isfinite(worst - mm), worst - mm, np.inf)
        wt = np.where(held & np.isfinite(worst), worst, np.inf)
        z = wz.min(axis=1) - cin
        trough = np.minimum(wt.min(axis=1), 0.0) - cin
        ok = filled & np.isfinite(r) & np.isfinite(z) & (s != 0)
        if hedge == 'spot':
            ok &= (s < 0) & np.isfinite(Be) & np.isfinite(Bx)      # spot hedge only for short perp / long spot
        out['r'][sl] = np.where(ok, r, np.nan)
        out['z'][sl] = np.where(ok, z, np.nan)
        out['trough'][sl] = np.where(ok, trough, np.nan)
        out['fund'][sl] = np.where(ok, fund, np.nan)
        out['px'][sl] = np.where(ok, coin, np.nan)
        out['hdg'][sl] = np.where(ok, hdg, np.nan)
        out['cost'][sl] = np.where(ok, cin + cout, np.nan)
        out['traded'][sl] = ok
    return out


_SPOT = {}


def trades_spot(ev, W, a, b, direction, sig, execm):
    """Spot-hedged version: runs trades() on the events that have Binance spot 1m data (positive funding,
    short perp / long spot) and scatters the results back to the full event index."""
    if not _SPOT:
        ms = pd.read_parquet(os.path.join(D, 'win_meta_spot.parquet'))
        WS = np.load(os.path.join(D, 'win_spot.npy'), mmap_mode='r')
        key = ev[['sym', 't']].reset_index().merge(ms, on=['sym', 't'], how='inner')
        _SPOT['idx'] = key['index'].values
        _SPOT['WS'] = np.asarray(WS[key['row'].values][:, :, :4])
    idx = _SPOT['idx']
    sub = ev.iloc[idx].reset_index(drop=True)
    tr = trades(sub, np.asarray(W[idx]), _SPOT['WS'], a, b, direction, sig, 'spot', execm)
    n = len(ev)
    out = {}
    for k, v in tr.items():
        full = np.zeros(n, bool) if v.dtype == bool else np.full(n, np.nan)
        full[idx] = v
        out[k] = full
    return out


def portfolio(T, absig, r, z, trough, elig, thr, K, L, order=None):
    """Events already filtered by validity. Returns per-settlement-time arrays.
    order = np.lexsort((-absig, T)) precomputed (events sorted by time, then by |signal| descending)."""
    m = elig & (absig >= thr) & np.isfinite(r)
    if order is None:
        order = np.lexsort((-absig, T))
    idx = order[m[order]]
    if len(idx) == 0:
        return None
    Ti = T[idx]
    newg = np.r_[True, Ti[1:] != Ti[:-1]]
    gstart = np.nonzero(newg)[0]
    gid = np.cumsum(newg) - 1
    rank = np.arange(len(idx)) - gstart[gid]
    keep = rank < K
    idx, gid = idx[keep], gid[keep]
    Tg = T[idx][np.r_[True, gid[1:] != gid[:-1]]]
    liq = z[idx] <= -1.0 / L
    val = np.where(liq, 0.0, 1.0 + L * r[idx])
    val = np.maximum(val, 0.0)
    tv = np.where(liq, 0.0, np.maximum(1.0 + L * trough[idx], 0.0))
    cnt = np.bincount(gid)
    g = np.bincount(gid, weights=val) / cnt
    tr = np.bincount(gid, weights=tv) / cnt
    nl = np.bincount(gid, weights=liq.astype(float))
    return Tg, g, tr, cnt, nl


def metrics(Tg, g, tr, cnt, nl, t0, t1):
    t0ms, t1ms = t0.value // 10 ** 6, t1.value // 10 ** 6
    ndays = (t1 - t0).days
    res = {'trades': 0, 'liq': 0, 'cagr': 0.0, 'maxdd': 0.0, 'worst_day': 0.0, 'sharpe': 0.0, 'final': 1.0, 'years': {}}
    if Tg is None:
        return res
    m = (Tg >= t0ms) & (Tg < t1ms)
    if not m.any():
        return res
    Tg, g, tr, cnt, nl = Tg[m], g[m], tr[m], cnt[m], nl[m]
    E = np.cumprod(g)
    Eb = np.r_[1.0, E[:-1]]
    peak = np.maximum.accumulate(np.r_[1.0, E])[:-1]
    dd = np.minimum(Eb * tr, E) / peak - 1.0
    maxdd = float(min(dd.min(), 0.0))
    day = (Tg - t0ms) // 86400000
    dlog = np.zeros(ndays)
    with np.errstate(divide='ignore'):
        np.add.at(dlog, day, np.log(np.maximum(g, 1e-300)))
    dret = np.expm1(dlog)
    final = float(E[-1])
    cagr = final ** (365.0 / ndays) - 1.0 if final > 0 else -1.0
    sd = dret.std()
    sharpe = float(dret.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0
    years = {}
    yrs = pd.to_datetime(Tg, unit='ms').year
    Ey = 1.0
    for y in np.unique(yrs):
        e1 = float(E[yrs == y][-1])
        years[int(y)] = (e1 / Ey - 1.0) if Ey > 0 else None      # None: account already ruined
        Ey = e1
    return {'trades': int(cnt.sum()), 'liq': int(nl.sum()), 'cagr': float(cagr), 'maxdd': maxdd,
            'worst_day': float(dret.min()), 'sharpe': sharpe, 'final': final, 'years': years}


def eligibility(ev, L, hedge, universe):
    beta = ev.beta.values if hedge == 'btc' else 0.0
    e = L * (1.0 / ev.maxlev.values + beta / 100.0) <= 1.0 + 1e-9
    if universe == 'okx':
        e = e & ev.okx_pit.values
    return e


def run_grid(specs, thrs, Ks, sigs, hedges, execs, universes, tag):
    ev, W, WB = load()
    T = ev.t.values
    rows = []
    cache = {}
    t_start = time.time()
    for (a, b, direction), sig, hedge, execm in itertools.product(specs, sigs, hedges, execs):
        if hedge == 'spot':
            tr = trades_spot(ev, W, a, b, direction, sig, execm)
        else:
            tr = trades(ev, W, WB, a, b, direction, sig, hedge, execm)
        absig = np.nan_to_num(np.abs(signal(ev, sig, a)))
        key = (sig, a if sig == 'pred' else 0)
        if key not in cache:
            cache[key] = np.lexsort((-absig, T))
        order = cache[key]
        print(f'spec {a},{b},{direction} {sig} {hedge} {execm}: traded {tr["traded"].sum()} mean r(bp) '
              f'{np.nanmean(tr["r"]) * 1e4:.2f} fund {np.nanmean(tr["fund"]) * 1e4:.2f} px {np.nanmean(tr["px"]) * 1e4:.2f} '
              f'hdg {np.nanmean(tr["hdg"]) * 1e4:.2f} cost {np.nanmean(tr["cost"]) * 1e4:.2f}  [{time.time() - t_start:.0f}s]', flush=True)
        for universe, thr, K, L in itertools.product(universes, thrs, Ks, LEVS):
            el = eligibility(ev, L, hedge, universe) & tr['traded']
            P = portfolio(T, absig, tr['r'], tr['z'], tr['trough'], el, thr, K, L, order)
            Pn = P if P is not None else (None,) * 5
            mi = metrics(*Pn, IS0, IS1)
            mo = metrics(*Pn, IS1, OOS1)
            rows.append({'family': tag, 'a': a, 'b': b, 'dir': direction, 'sig': sig, 'hedge': hedge, 'exec': execm,
                         'universe': universe, 'thr': thr, 'K': K, 'L': L,
                         'cagr_is': mi['cagr'], 'cagr_oos': mo['cagr'], 'maxdd_is': mi['maxdd'], 'maxdd_oos': mo['maxdd'],
                         'worst_day_is': mi['worst_day'], 'worst_day_oos': mo['worst_day'],
                         'sharpe_is': mi['sharpe'], 'sharpe_oos': mo['sharpe'], 'liq_is': mi['liq'], 'liq_oos': mo['liq'],
                         'trades_is': mi['trades'], 'trades_oos': mo['trades'], 'final_is': mi['final'], 'final_oos': mo['final'],
                         'years': json.dumps({**mi['years'], **mo['years']})})
    return pd.DataFrame(rows)


if __name__ == '__main__':
    which = sys.argv[1]
    if which == 'capture':      # family (i)
        specs = [(a, b, 'recv') for a in (-30, -15, -5, -1) for b in (1, 5, 15, 30)]
        df = run_grid(specs, thrs=[0.5, 1.0, 2.0, 4.0], Ks=[1, 3, 10], sigs=['lag', 'oracle'],
                      hedges=['btc', 'none'], execs=['taker', 'maker'], universes=['all', 'okx'], tag='capture')
    elif which == 'capture_pred':   # family (i) with the decision-time PREDICTED funding
        specs = [(a, b, 'recv') for a in (-30, -15, -5, -1) for b in (1, 5, 15, 30)]
        df = run_grid(specs, thrs=[0.5, 1.0, 2.0, 4.0], Ks=[1, 3, 10], sigs=['pred'],
                      hedges=['btc', 'none'], execs=['taker', 'maker'], universes=['all', 'okx'], tag='capture')
    elif which == 'drift_pred':     # family (ii) with predicted funding (pre) / realised-at-t funding (post)
        specs = [(a, -1, d) for a in (-60, -30, -15, -5) for d in ('recv', 'pay')] + \
                [(1, b, d) for b in (5, 15, 30, 60) for d in ('recv', 'pay')]
        df = run_grid(specs, thrs=[0.5, 1.0, 2.0, 4.0], Ks=[1, 3, 10], sigs=['pred'],
                      hedges=['btc', 'none'], execs=['taker', 'maker'], universes=['all', 'okx'], tag='drift')
    elif which == 'capture_spot':   # family (i), spot-hedged (positive funding only), lag and pred signals
        specs = [(a, b, 'recv') for a in (-30, -15, -5, -1) for b in (1, 5, 15, 30)]
        df = run_grid(specs, thrs=[0.5, 1.0, 2.0, 4.0], Ks=[1, 3, 10], sigs=['lag', 'pred', 'oracle'],
                      hedges=['spot'], execs=['taker', 'maker'], universes=['all', 'okx'], tag='capture')
    elif which == 'drift':      # family (ii)
        specs = [(a, -1, d) for a in (-60, -30, -15, -5) for d in ('recv', 'pay')] + \
                [(1, b, d) for b in (5, 15, 30, 60) for d in ('recv', 'pay')]
        df = run_grid(specs, thrs=[0.5, 1.0, 2.0, 4.0], Ks=[1, 3, 10], sigs=['lag'],
                      hedges=['btc', 'none'], execs=['taker', 'maker'], universes=['all', 'okx'], tag='drift')
    df.to_parquet(os.path.join(D, f'grid_{which}.parquet'))
    df.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), f'grid_{which}.csv.gz'), index=False)
    print('saved', len(df))
