"""Diversified daily time-series momentum (managed-futures recipe) on point-in-time liquid OKX/Binance perps.

Unit portfolio ("1x"): long/short, per-coin weight = signal / annualised vol, scaled to an ex-ante portfolio vol of
SIGMA_STAR (historical simulation of today's weights on the last 120 days, EWMA hl 30) and capped at GROSS_CAP = 1.0
(never more notional than equity at 1x). Leverage L multiplies every position.

Timing: signal from closes up to day t (00:00 UTC of t+1), traded at that close (taker fee + slippage), held over day t+1
(return C[t+1]/C[t]-1 minus funding of the events in (t+1 00:00, t+2 00:00]).  Latency variant: one extra daily bar.
"""
import json
import numpy as np, pandas as pd

SP = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad'
W = f'{SP}/tsmom_div'
SIGMA_STAR = 0.20
GROSS_CAP = 1.0
FEE_TAKER = 0.0005
MIN_HIST = 150          # days of Binance history required before a coin is eligible
IS_START, IS_END = '2022-01-01', '2024-12-31'
OOS_START, OOS_END = '2025-01-01', '2026-08-31'

_P = {}


def load():
    if _P:
        return _P
    z = np.load(f'{W}/panel.npz', allow_pickle=False)
    P = {k: z[k] for k in z.files}
    P['dates'] = pd.DatetimeIndex(pd.to_datetime(P['dates'])).tz_localize('UTC')
    P['syms'] = list(P['syms'])
    C = P['C'].copy()
    live = P['live']
    # history length (count of live days so far)
    P['hist'] = np.cumsum(live, axis=0)
    # simple returns close-to-close; NaN where not live on both days
    Cf = pd.DataFrame(C).ffill().values
    r = np.full_like(C, np.nan)
    r[1:] = C[1:] / C[:-1] - 1
    r[~live] = np.nan
    r[1:][~live[:-1]] = np.nan
    P['r'] = r
    P['Cf'] = Cf
    # intrabar extremes relative to previous close (for longs: low, for shorts: high)
    lo = np.full_like(C, np.nan); hi = np.full_like(C, np.nan)
    lo[1:] = P['L'][1:] / C[:-1] - 1
    hi[1:] = P['H'][1:] / C[:-1] - 1
    P['lo'] = np.where(np.isnan(r), 0.0, lo)
    P['hi'] = np.where(np.isnan(r), 0.0, hi)
    # trailing 30-day mean quote volume (known at end of day t)
    qv = pd.DataFrame(np.where(live, P['QV'], 0.0))
    P['qv30'] = qv.rolling(30, min_periods=20).mean().values
    _P.update(P)
    return P


def universe(N, exclude=None):
    """Monthly point-in-time membership: on the first day of each month, the top-N symbols by trailing 30-day
    Binance quote volume among those live on Binance and trading on OKX that day with >= MIN_HIST days of history.
    Membership is additionally switched off on any day the coin is not live on Binance or not trading on OKX."""
    P = load()
    D = P['dates']
    T, S = P['C'].shape
    mem = np.zeros((T, S), bool)
    rank = np.full((T, S), np.nan)
    cur = np.zeros(S, bool); cur_rank = np.full(S, np.nan)
    for t in range(T):
        if t == 0 or D[t].month != D[t - 1].month:
            elig = P['live'][t] & P['okx'][t] & (P['hist'][t] >= MIN_HIST) & np.isfinite(P['qv30'][t])
            if exclude is not None:
                elig = elig & ~exclude
            q = np.where(elig, P['qv30'][t], -1)
            order = np.argsort(-q)
            cur = np.zeros(S, bool); cur_rank = np.full(S, np.nan)
            k = 0
            for j in order:
                if q[j] <= 0 or k >= N:
                    break
                cur[j] = True; cur_rank[j] = k + 1; k += 1
        m = cur & P['live'][t] & P['okx'][t]
        mem[t] = m
        rank[t] = np.where(m, cur_rank, np.nan)
    return mem, rank


def ewm_vol(hl):
    """EWMA daily vol of close-to-close returns known at end of day t (annualised)."""
    P = load()
    r = pd.DataFrame(P['r'])
    v = (r ** 2).ewm(halflife=hl, min_periods=20, ignore_na=True).mean()
    return np.sqrt(v.values * 365)


def signal(lbs, kind, vol_ann):
    P = load()
    lc = np.log(P['Cf'])
    T, S = lc.shape
    sig = np.zeros((T, S))
    sd = vol_ann / np.sqrt(365)
    for h in lbs:
        rh = np.full((T, S), np.nan)
        rh[h:] = lc[h:] - lc[:-h]
        if kind == 'sign':
            s = np.sign(rh)
        else:  # vol-normalised, clipped z in [-1, 1]
            s = np.clip(rh / (sd * np.sqrt(h)) / 2.0, -1, 1)
        sig += np.nan_to_num(s)
    return sig / len(lbs)


def slip_bp(rank, syms):
    """Slippage per side (bp) by point-in-time liquidity rank: BTC/ETH 1bp, rank<=10 3bp, <=20 5bp, else 8bp."""
    s = np.where(rank <= 10, 3.0, np.where(rank <= 20, 5.0, 8.0))
    for j, n in enumerate(syms):
        if n in ('BTCUSDT', 'ETHUSDT'):
            s[:, j] = 1.0
    return s * 1e-4


def target_weights(cfg, cache=None):
    """Unit (1x) target weights decided at end of each day (T x S)."""
    P = load()
    cache = cache if cache is not None else {}
    exc = cfg.get('exclude')
    key_u = ('u', cfg['N'], exc)
    if key_u not in cache:
        ex = None
        if exc:
            ex = np.array([s == exc for s in P['syms']])
        cache[key_u] = universe(cfg['N'], ex)
    mem, rank = cache[key_u]
    key_v = ('v', cfg['hl'])
    if key_v not in cache:
        cache[key_v] = ewm_vol(cfg['hl'])
    vol = cache[key_v]
    key_s = ('s', tuple(cfg['lbs']), cfg['kind'], cfg['hl'])
    if key_s not in cache:
        cache[key_s] = signal(cfg['lbs'], cfg['kind'], vol)
    sig = cache[key_s]
    r0 = np.nan_to_num(P['r'])
    T, S = sig.shape
    Wt = np.zeros((T, S))
    ew = 0.5 ** (np.arange(120)[::-1] / 30.0)
    ew = ew / ew.sum()
    for t in range(130, T):
        m = mem[t] & np.isfinite(vol[t]) & (vol[t] > 0)
        if not m.any():
            continue
        u = np.zeros(S)
        u[m] = sig[t, m] / vol[t, m]
        if not np.any(u):
            continue
        idx = np.where(m)[0]
        pr = r0[t - 119:t + 1][:, idx] @ u[idx]
        pv = np.sqrt(np.sum(ew * pr ** 2) * 365)
        if pv <= 0:
            continue
        w = u * (SIGMA_STAR / pv)
        g = np.abs(w).sum()
        if g > GROSS_CAP:
            w *= GROSS_CAP / g
        Wt[t] = w
    return Wt, mem, rank


def backtest(Wt, rank, band=0.0, cost_mult=1.0, lag=0, start=IS_START, end=OOS_END, L=1.0, E0=None,
             lots=None, mmr=None, imr=None, record=False):
    """Daily simulation at leverage L (positions = L * weights * equity).
    band: trade coin j only if |target - current| > band * gross_target / n_active (exits always traded).
    lots: optional (T x S) contract value in USDT (ctVal*price) and minimum-size array for small-account rounding.
    Returns DataFrame (daily): ret, gross, turnover, cost, funding, ib (intrabar min return), liq flag."""
    P = load()
    D = P['dates']
    t0 = D.searchsorted(pd.Timestamp(start, tz='UTC'))
    t1 = D.searchsorted(pd.Timestamp(end, tz='UTC'), side='right')
    r = np.nan_to_num(P['r']); F = P['F']; lo = P['lo']; hi = P['hi']
    slip = slip_bp(np.nan_to_num(rank, nan=40), P['syms'])
    fee = FEE_TAKER
    S = Wt.shape[1]
    if lag:
        Wt = np.vstack([np.zeros((lag, S)), Wt[:-lag]])
    w = np.zeros(S)            # current holdings as fraction of equity (already includes L)
    E = 1.0 if E0 is None else float(E0)
    rows = []
    liq = False
    for t in range(t0 - 1, t1 - 1):
        # --- rebalance at close of day t
        tgt = L * Wt[t]
        if lots is not None:
            cv, minq = lots
            with np.errstate(divide='ignore', invalid='ignore'):
                q = np.where(cv[t] > 0, tgt * E / cv[t], 0.0)
            q = np.round(q / minq[t]) * minq[t]
            q = np.nan_to_num(q)
            tgt = q * cv[t] / E
        if band > 0:
            na = max((np.abs(tgt) > 0).sum(), 1)
            thr = band * np.abs(tgt).sum() / na
            trade = (np.abs(tgt - w) > thr) | ((tgt == 0) & (w != 0))
            new = np.where(trade, tgt, w)
        else:
            new = tgt
        dw = np.abs(new - w)
        cost = cost_mult * np.sum(dw * (fee + slip[t]))
        w = new
        # --- hold over day t+1
        pnl = np.sum(w * (r[t + 1] - F[t + 1]))
        ib = np.sum(np.minimum(w * lo[t + 1], w * hi[t + 1]))     # all adverse extremes at once (conservative)
        ib = min(ib - cost, pnl - cost, -cost)
        ret = pnl - cost
        gross = np.abs(w).sum()
        mm = float(np.sum(np.abs(w) * mmr)) if mmr is not None else np.nan
        im = float(np.sum(np.abs(w) * imr)) if imr is not None else np.nan
        rows.append((D[t + 1], ret, gross, dw.sum(), cost, -np.sum(w * F[t + 1]), ib, mm, im, int((dw > 1e-12).sum())))
        # drift weights
        grow = 1 + ret
        if grow <= 0:
            w = np.zeros(S)
            liq = True
            break
        w = w * (1 + r[t + 1]) / grow
        E *= grow
    out = pd.DataFrame(rows, columns=['d', 'ret', 'gross', 'turnover', 'cost', 'funding', 'ib', 'mm', 'im', 'ntrades']).set_index('d')
    out.attrs['wiped'] = liq
    return out


def sharpe(x):
    x = pd.Series(x).dropna()
    return float(x.mean() / x.std() * np.sqrt(365)) if x.std() > 0 else 0.0


def stats(df, a, b):
    x = df.loc[a:b, 'ret']
    eq = (1 + x).cumprod()
    yrs = len(x) / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if len(x) and eq.iloc[-1] > 0 else -1
    dd = float((1 - eq / eq.cummax()).max()) if len(x) else 0
    return dict(sharpe=sharpe(x), cagr=float(cagr), vol=float(x.std() * np.sqrt(365)), maxdd_close=dd,
                gross=float(df.loc[a:b, 'gross'].mean()), turnover_ann=float(df.loc[a:b, 'turnover'].mean() * 365),
                cost_ann=float(df.loc[a:b, 'cost'].mean() * 365), fund_ann=float(df.loc[a:b, 'funding'].mean() * 365))


def okx_arrays(default_mmr=0.02, default_maxlev=20.0, default_ct_usd=10.0):
    """Per-symbol OKX tier-1 MMR / IMR (1/maxLever) and per-day contract value in USDT + min size (in contracts,
    as lot multiples) from today's OKX specs applied to historical Binance prices (Binance '1000X' = 1000 units)."""
    P = load()
    spec = json.load(open(f'{W}/okx_spec_now.json'))
    syms = P['syms']
    S = len(syms)
    mmr = np.full(S, default_mmr); imr = np.full(S, 1 / default_maxlev)
    cv = np.zeros_like(P['Cf']); lot = np.ones_like(P['Cf']); minq = np.ones_like(P['Cf'])
    for j, s in enumerate(syms):
        sp = spec.get(s)
        mult = 1.0
        for pre, m in (('1000000', 1e6), ('1000', 1e3), ('1M', 1e6)):
            if s.startswith(pre) and sp and not sp['instId'].startswith(s[:-4]):
                mult = m
                break
        if sp and sp.get('mmr1') is not None:
            mmr[j] = sp['mmr1']; imr[j] = 1.0 / sp['maxLever1']
        if sp:
            cv[:, j] = sp['ctVal'] * P['Cf'][:, j] / mult
            lot[:, j] = sp['lotSz']
            minq[:, j] = max(sp['minSz'], sp['lotSz'])
        else:
            cv[:, j] = default_ct_usd
    cv = np.nan_to_num(cv)
    # rounding unit: contracts are traded in lotSz steps with minimum minSz; we round to lotSz and drop below minSz
    return mmr, imr, cv, lot, minq
