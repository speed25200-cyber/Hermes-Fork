"""Pairs / cointegration statistical arbitrage on USDT-M perpetuals at leverage 1..20x (OKX VIP0 costs).

DATA (real, point-in-time): Binance USDT-M archive (data.binance.vision) 1h last-price klines, 1h mark-price klines,
funding-rate events, and 1d klines for the liquidity ranking; universe restricted to coins that have/had an OKX
USDT-SWAP.  OKX tier-1 maintenance / initial margin from /api/v5/public/position-tiers (fetched 2026-09-24).
Binance prices/funding stand in for OKX (same contracts, prices within a few bp; funding differs slightly).

UNIVERSE / FORMATION (walk-forward, monthly, past data only): at 00:00 UTC on the 1st of each month, the 30 perps with
the largest trailing-30-day quote volume (delisted coins included: LUNA, FTT-like cases are in the archive).
Pairs are ranked on the past Wf days of hourly log closes:
   coint : Engle-Granger ADF t-stat of the spread (must be <= -3.34, 5% EG critical value), most negative first
   corr  : correlation of hourly log returns (>= 0.5), highest first
   fixed : ETH/BTC, SOL/ETH, LTC/BCH, BNB/ETH, DOGE/1000SHIB (pre-specified, coin reuse allowed)
greedy, no coin used twice (coint/corr), top K.  Hedge ratio beta (fixed for the month) from the same window:
   lvl : OLS of log A on log B (levels)      ret : OLS of hourly log returns of A on B
Spread s = log A - beta log B; z = (s - rolling mean_Wz) / rolling std_Wz (window ends at the current close).

TRADING (1h bars): signal on bar close, execution at the NEXT bar open with taker fees on both legs + slippage.
   enter long spread (long A, short beta*A notional of B) when z <= -z_in, short when z >= z_in
   exit when z crosses back to -z_out / +z_out; stop when |z| >= z_stop (pair then blocked until next formation);
   time stop after Wz hours; forced exit when the pair leaves the monthly selection; exit at last price if a leg
   stops trading (delisting).
LEVERAGE L = per-leg average notional / equity: each of the K slots gets (|nA|+|nB|)/2 = L*E/K at entry, so with all
   K slots filled gross notional = 2*L*E.  Initial margin must fit: sum notional*imr <= equity (else size is cut).
MARGIN: 'cross' = one OKX cross-margin account for all pairs; 'sleeve' = one OKX sub-account per slot (cross inside),
   equity re-split equally at each monthly formation (free internal transfers).
LIQUIDATION (conservative): every bar, long legs marked at their MARK-price low and short legs at their MARK-price
   high SIMULTANEOUSLY (the legs' extremes are not simultaneous, so this is a lower bound on equity); liquidation if
   worst equity <= sum(mmr + taker fee) * notional.  A liquidated account/sleeve loses everything it holds
   (maintenance margin goes to the insurance fund), as in round 1.  Cross: the run is dead; sleeve: refilled at the
   next monthly re-split.
FUNDING: real funding events on both legs (long pays rate*notional, short receives).
COSTS: taker 0.05% per leg per side; slippage base 1 bp BTC/ETH, 3 bp other top-10 coins, 5 bp others,
   + 2% of the execution bar's high-low range (fast markets).
PROTOCOL: every tunable chosen on 2022-01-01..2024-12-31 (IS run, equity 1); 2025-01-01..2026-08-31 reported as an
   independent OOS run (equity 1, flat start, formation still walk-forward on past data).
"""
import os, sys, json, time, itertools, math
import numpy as np, pandas as pd
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, 'data')
OUT = os.path.join(HERE, 'out')
os.makedirs(OUT, exist_ok=True)

G0, G1 = pd.Timestamp('2021-08-01'), pd.Timestamp('2026-09-01')
GRID = pd.date_range(G0, G1, freq='h', inclusive='left')
NB = len(GRID)
IS0, IS1 = pd.Timestamp('2022-01-01'), pd.Timestamp('2025-01-01')
OOS0, OOS1 = pd.Timestamp('2025-01-01'), pd.Timestamp('2026-09-01')
FORM_DATES = pd.date_range('2022-01-01', '2026-08-01', freq='MS')
FEE_T = 0.0005
RANGE_SLIP = 0.02
LEVS = [1, 3, 5, 10, 15, 20]
EXCL = {'USDCUSDT', 'BUSDUSDT', 'TUSDUSDT', 'FDUSDUSDT', 'USDPUSDT', 'DAIUSDT', 'XAUTUSDT'}
FIXED = [('ETHUSDT', 'BTCUSDT'), ('SOLUSDT', 'ETHUSDT'), ('LTCUSDT', 'BCHUSDT'), ('BNBUSDT', 'ETHUSDT'),
         ('DOGEUSDT', '1000SHIBUSDT')]


# ------------------------------------------------------------------ data
def load_panel():
    syms = sorted(f[:-8] for f in os.listdir(os.path.join(D, 'h')))
    n = len(syms)
    A = {k: np.full((n, NB), np.nan, dtype=np.float32) for k in ['o', 'h', 'l', 'c', 'mh', 'ml']}
    FR = np.zeros((n, NB), dtype=np.float32)
    t0 = G0.value // 10**6
    for j, s in enumerate(syms):
        d = pd.read_parquet(os.path.join(D, 'h', s + '.parquet'))
        idx = ((d.t.values - t0) // 3600000).astype(np.int64)
        ok = (idx >= 0) & (idx < NB) & (d.t.values % 3600000 == 0)
        for k in A:
            A[k][j, idx[ok]] = d[k].values[ok]
        fp = os.path.join(D, 'f', s + '.parquet')
        if os.path.exists(fp):
            f = pd.read_parquet(fp)
            # event at time T is paid by positions held at T -> bar whose close is T (open T-1h)
            th = (np.round((f.t.values - t0) / 3600000.0)).astype(np.int64) - 1
            ok = (th >= 0) & (th < NB)
            np.add.at(FR[j], th[ok], f.rate.values[ok].astype(np.float32))
    # mark missing -> last price
    for a, b in [('mh', 'h'), ('ml', 'l')]:
        m = np.isnan(A[a]) & ~np.isnan(A[b])
        A[a][m] = A[b][m]
    # mark extremes must bracket; take the wider of mark and last (conservative)
    A['mh'] = np.fmax(A['mh'], A['h']); A['ml'] = np.fmin(A['ml'], A['l'])
    return dict(syms=np.array(syms), fr=FR, **A)


def load_tiers(syms):
    t = pd.read_csv(os.path.join(D, 'okx_tiers.csv')).set_index('sym')
    mmr = np.array([t.mmr.get(s, np.nan) for s in syms], float)
    imr = np.array([t.imr.get(s, np.nan) for s in syms], float)
    mmr = np.where(np.isnan(mmr), 0.02, mmr)      # delisted / renamed: median OKX alt tier 1 (2%, 20x)
    imr = np.where(np.isnan(imr), 0.05, imr)
    return mmr, imr


def monthly_universe(syms):
    d = pd.read_parquet(os.path.join(D, 'daily_1d.parquet'))
    d = d[~d.sym.isin(EXCL) & d.sym.isin(set(syms))]
    U = {}
    for t in FORM_DATES:
        w = d[(d.t >= t - pd.Timedelta(days=30)) & (d.t < t)]
        U[t] = list(w.groupby('sym').qv.sum().sort_values(ascending=False).head(30).index)
    return U


# ------------------------------------------------------------------ formation
def adf_t(E):
    """ADF(1) t-stat with constant for each column of E (n x P)."""
    dE = np.diff(E, axis=0)
    y = dE[1:]; x1 = E[1:-1]; x2 = dE[:-1]
    m = y.shape[0]
    S1 = x1.sum(0); S2 = x2.sum(0); Sy = y.sum(0)
    S11 = (x1 * x1).sum(0); S22 = (x2 * x2).sum(0); S12 = (x1 * x2).sum(0)
    S1y = (x1 * y).sum(0); S2y = (x2 * y).sum(0); Syy = (y * y).sum(0)
    P = E.shape[1]
    XtX = np.empty((P, 3, 3)); Xty = np.empty((P, 3))
    XtX[:, 0, 0] = m; XtX[:, 0, 1] = XtX[:, 1, 0] = S1; XtX[:, 0, 2] = XtX[:, 2, 0] = S2
    XtX[:, 1, 1] = S11; XtX[:, 1, 2] = XtX[:, 2, 1] = S12; XtX[:, 2, 2] = S22
    Xty[:, 0] = Sy; Xty[:, 1] = S1y; Xty[:, 2] = S2y
    inv = np.linalg.inv(XtX)
    b = np.einsum('pij,pj->pi', inv, Xty)
    rss = Syy - (b * Xty).sum(1)
    s2 = rss / (m - 3)
    se = np.sqrt(np.maximum(s2 * inv[:, 1, 1], 1e-300))
    return b[:, 1] / se, b[:, 1]


def formation(P, t, syms_idx, Wf):
    """Pair statistics on [t - Wf days, t) for the eligible symbols. Returns DataFrame of ordered pairs."""
    i1 = GRID.get_loc(t); i0 = i1 - Wf * 24
    C = P['c'][syms_idx, i0:i1].astype(np.float64).T          # n x N
    ok = (np.isnan(C).mean(0) <= 0.05) & ~np.isnan(C[-24:]).all(0) & ~np.isnan(C[:24]).all(0)
    idx = np.array(syms_idx)[ok]; C = C[:, ok]
    if C.shape[1] < 2:
        return pd.DataFrame()
    X = pd.DataFrame(np.log(C)).ffill().bfill().values
    R = np.diff(X, axis=0)
    N = X.shape[1]
    corr = np.corrcoef(R.T)
    ii, jj = np.where(~np.eye(N, dtype=bool))                 # ordered pairs (A=i, B=j)
    xa, xb = X[:, ii], X[:, jj]
    xa_c = xa - xa.mean(0); xb_c = xb - xb.mean(0)
    b_lvl = (xa_c * xb_c).sum(0) / (xb_c * xb_c).sum(0)
    ra, rb = R[:, ii], R[:, jj]
    ra_c = ra - ra.mean(0); rb_c = rb - rb.mean(0)
    b_ret = (ra_c * rb_c).sum(0) / (rb_c * rb_c).sum(0)
    t_lvl, _ = adf_t(xa - b_lvl * xb)
    t_ret, g_ret = adf_t(xa - b_ret * xb)
    return pd.DataFrame(dict(a=idx[ii], b=idx[jj], corr=corr[ii, jj], b_lvl=b_lvl, b_ret=b_ret, t_lvl=t_lvl,
                             t_ret=t_ret))


def select(df, method, hedge, K, sym_names):
    if df.empty:
        return []
    bcol, tcol = 'b_' + hedge, 't_' + hedge
    d = df[(df[bcol] >= 0.2) & (df[bcol] <= 5.0)]
    if method == 'fixed':
        name2 = {s: i for i, s in enumerate(sym_names)}
        out = []
        for a, b in FIXED:
            if a in name2 and b in name2:
                r = d[(d.a == name2[a]) & (d.b == name2[b])]
                if len(r):
                    out.append((int(name2[a]), int(name2[b]), float(r[bcol].iloc[0])))
        return out
    if method == 'coint':
        d = d[(d[tcol] <= -3.34) & (d['corr'] >= 0.5)].sort_values(tcol)
    else:
        # corr: symmetric; take the ordering with the better ADF for each unordered pair
        d = d[d['corr'] >= 0.5].sort_values(['corr', tcol], ascending=[False, True])
    used, out = set(), []
    for a, b, bt in zip(d.a.values, d.b.values, d[bcol].values):
        if a in used or b in used:
            continue
        used.add(a); used.add(b)
        out.append((int(a), int(b), float(bt)))
        if len(out) >= K:
            break
    return out


def build_selections(P, U, first_bar):
    """sel[(method, hedge, Wf)][t] = list of (a, b, beta) of length <= 10 (fixed: <= 5)."""
    cache = os.path.join(OUT, 'selections.json')
    if os.path.exists(cache):
        raw = json.load(open(cache))
        return {tuple(k.split('|')[:2]) + (int(k.split('|')[2]),): {pd.Timestamp(t): v for t, v in d.items()}
                for k, d in raw.items()}
    names = list(P['syms']); name2 = {s: i for i, s in enumerate(names)}
    sel = {}
    for Wf in [60, 120]:
        for t in FORM_DATES:
            i1 = GRID.get_loc(t)
            elig = [name2[s] for s in U[t] if first_bar[name2[s]] <= i1 - Wf * 24]
            # fixed pairs are always evaluated even if a coin drops out of the top 30
            fx = sorted(set(elig) | {name2[s] for p in FIXED for s in p if s in name2 and first_bar[name2[s]] <= i1 - Wf * 24})
            df = formation(P, t, elig, Wf)
            dfx = formation(P, t, fx, Wf)
            for hedge in ['lvl', 'ret']:
                for method in ['coint', 'corr']:
                    sel.setdefault((method, hedge, Wf), {})[t] = select(df, method, hedge, 10, names)
                sel.setdefault(('fixed', hedge, Wf), {})[t] = select(dfx, 'fixed', hedge, 5, names)
        print('formation Wf', Wf, 'done', flush=True)
    json.dump({f'{k[0]}|{k[1]}|{k[2]}': {str(t): v for t, v in d.items()} for k, d in sel.items()},
              open(cache, 'w'))
    return sel


# ------------------------------------------------------------------ slot arrays
def slot_arrays(P, U, sel_m, K, Wz, base_slip_rank):
    """Per slot (K) and bar: A, B symbol index (-1 inactive), beta, pair id, z-score, base slippage of each leg."""
    names = list(P['syms'])
    PA = np.full((K, NB), -1, np.int32); PB = np.full((K, NB), -1, np.int32)
    BE = np.zeros((K, NB)); Z = np.full((K, NB), np.nan)
    SA = np.zeros((K, NB)); SB = np.zeros((K, NB))
    NEWM = np.zeros(NB, np.int8)
    prev = {}
    logc = P['logc']
    for m, t in enumerate(FORM_DATES):
        i0 = GRID.get_loc(t)
        i1 = GRID.get_loc(FORM_DATES[m + 1]) if m + 1 < len(FORM_DATES) else NB
        NEWM[i0] = 1
        pairs = sel_m[t][:K]
        # keep slots for carried-over pairs
        slots = {}
        free = list(range(K))
        for (a, b, be) in pairs:
            if (a, b) in prev:
                slots[(a, b)] = prev[(a, b)]; free.remove(prev[(a, b)])
        for (a, b, be) in pairs:
            if (a, b) not in slots:
                slots[(a, b)] = free.pop(0)
        rk = {name: r for r, name in enumerate(U[t])}
        for (a, b, be) in pairs:
            k = slots[(a, b)]
            j0 = max(0, i0 - Wz - 48)
            s = logc[a, j0:i1] - be * logc[b, j0:i1]
            ss = pd.Series(s)
            mu = ss.rolling(Wz, min_periods=int(Wz * 0.9)).mean().values
            sd = ss.rolling(Wz, min_periods=int(Wz * 0.9)).std().values
            z = (s - mu) / sd
            PA[k, i0:i1] = a; PB[k, i0:i1] = b; BE[k, i0:i1] = be
            Z[k, i0:i1] = z[i0 - j0:]
            SA[k, i0:i1] = base_slip_rank(names[a], rk.get(names[a], 99))
            SB[k, i0:i1] = base_slip_rank(names[b], rk.get(names[b], 99))
        prev = slots
    return PA, PB, BE, Z, SA, SB, NEWM


def base_slip(name, rank):
    if name in ('BTCUSDT', 'ETHUSDT'):
        return 0.0001
    return 0.0003 if rank < 10 else 0.0005


# ------------------------------------------------------------------ simulator
@njit(cache=True)
def sim(O, H, L, C, MH, ML, FR, DAY, i0, i1, nd, PA, PB, BE, Z, SA, SB, NEWM, mmr, imr,
        zin, zout, zstop, maxhold, lev, sleeve, fee, rslip, rec):
    K = PA.shape[0]
    nsym = O.shape[0]
    lastc = np.full(nsym, np.nan)
    # slot state
    pos = np.zeros(K, np.int64); qA = np.zeros(K); qB = np.zeros(K); eA = np.zeros(K); eB = np.zeros(K)
    ia = np.full(K, -1, np.int64); ib = np.full(K, -1, np.int64); tent = np.zeros(K, np.int64)
    pend = np.zeros(K, np.int64); blocked = np.zeros(K, np.int64)
    cash = np.zeros(K)                 # per-slot cash for sleeves; cross uses cash[0] only
    if sleeve:
        for k in range(K):
            cash[k] = 1.0 / K
    else:
        cash[0] = 1.0
    daily = np.full(nd, np.nan)
    ntr = 0; nliq = 0; nstop = 0; ntime = 0; nforce = 0
    fees = 0.0; fund = 0.0; bars_in = 0
    peak = 1.0; maxdd = 0.0; dead = False
    TR = np.zeros((rec, 9))      # slot, a, b, t_entry, t_exit, net pnl / equity basis, why, dir, basis
    nrec = 0
    s_eq0 = np.zeros(K); s_cost = np.zeros(K); s_why = np.zeros(K, np.int64)
    grosssum = 0.0
    for i in range(i0, i1):
        for s in range(nsym):
            if not np.isnan(C[s, i]):
                lastc[s] = C[s, i]
        if dead:
            daily[DAY[i]] = 0.0
            continue
        # ---- 1. executions at the open of bar i
        for k in range(K):
            if pos[k] != 0:
                force = (PA[k, i] != ia[k]) or (PB[k, i] != ib[k])
                if force or pend[k] == 2:
                    # close both legs at the open (or last known price if the leg stopped trading)
                    pa = O[ia[k], i]; pb = O[ib[k], i]
                    xa = 0.0; xb = 0.0
                    if np.isnan(pa):
                        pa = lastc[ia[k]]; xa = 0.01
                    if np.isnan(pb):
                        pb = lastc[ib[k]]; xb = 0.01
                    ra = H[ia[k], i] - L[ia[k], i]; rb = H[ib[k], i] - L[ib[k], i]
                    sla = SA[k, i - 1] + xa + (rslip * ra / pa if not np.isnan(ra) else 0.0)
                    slb = SB[k, i - 1] + xb + (rslip * rb / pb if not np.isnan(rb) else 0.0)
                    fa = pa * (1.0 - sla) if qA[k] > 0 else pa * (1.0 + sla)
                    fb = pb * (1.0 - slb) if qB[k] > 0 else pb * (1.0 + slb)
                    pnl = qA[k] * (fa - eA[k]) + qB[k] * (fb - eB[k])
                    fe = fee * (abs(qA[k]) * fa + abs(qB[k]) * fb)
                    kk = k if sleeve else 0
                    cash[kk] += pnl - fe
                    fees += fe
                    if nrec < rec:
                        TR[nrec, 0] = k; TR[nrec, 1] = ia[k]; TR[nrec, 2] = ib[k]; TR[nrec, 3] = tent[k]
                        TR[nrec, 4] = i; TR[nrec, 5] = (pnl - fe - s_cost[k]) / s_eq0[k]
                        TR[nrec, 6] = 6 if force else s_why[k]; TR[nrec, 7] = pos[k]; TR[nrec, 8] = s_eq0[k]
                        nrec += 1
                    if force:
                        nforce += 1
                    pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0
                    ia[k] = -1; ib[k] = -1
        if NEWM[i] == 1:
            for k in range(K):
                blocked[k] = 0
            if sleeve:
                # re-split total equity equally across sleeves (mark at the open)
                tot = 0.0
                eqk = np.zeros(K)
                for k in range(K):
                    e = cash[k]
                    if pos[k] != 0:
                        pa = O[ia[k], i]; pb = O[ib[k], i]
                        if np.isnan(pa):
                            pa = lastc[ia[k]]
                        if np.isnan(pb):
                            pb = lastc[ib[k]]
                        e += qA[k] * (pa - eA[k]) + qB[k] * (pb - eB[k])
                    eqk[k] = e; tot += e
                for k in range(K):
                    cash[k] += tot / K - eqk[k]
        # entries
        for k in range(K):
            if pend[k] == 1 or pend[k] == -1:
                d = pend[k]; pend[k] = 0
                a = PA[k, i]; b = PB[k, i]
                if a < 0 or blocked[k] == 1:
                    continue
                pa = O[a, i]; pb = O[b, i]
                if np.isnan(pa) or np.isnan(pb):
                    continue
                # equity basis at the open
                eq = 0.0; im_used = 0.0
                if sleeve:
                    eq = cash[k]
                else:
                    eq = cash[0]
                    for j in range(K):
                        if pos[j] != 0:
                            qa = O[ia[j], i]; qb = O[ib[j], i]
                            if np.isnan(qa):
                                qa = lastc[ia[j]]
                            if np.isnan(qb):
                                qb = lastc[ib[j]]
                            eq += qA[j] * (qa - eA[j]) + qB[j] * (qb - eB[j])
                            im_used += abs(qA[j]) * qa * imr[ia[j]] + abs(qB[j]) * qb * imr[ib[j]]
                if eq <= 0:
                    continue
                be = BE[k, i]
                per_leg = lev * eq if sleeve else lev * eq / K
                nA = 2.0 * per_leg / (1.0 + be); nB = be * nA
                need = nA * imr[a] + nB * imr[b]
                room = eq - im_used
                if room <= 0:
                    continue
                if need > room:
                    sc = room / need
                    nA *= sc; nB *= sc
                ra = H[a, i] - L[a, i]; rb = H[b, i] - L[b, i]
                sla = SA[k, i] + rslip * ra / pa; slb = SB[k, i] + rslip * rb / pb
                if d == 1:       # long A, short B
                    fa = pa * (1.0 + sla); fb = pb * (1.0 - slb)
                    qA[k] = nA / pa; qB[k] = -nB / pb
                else:
                    fa = pa * (1.0 - sla); fb = pb * (1.0 + slb)
                    qA[k] = -nA / pa; qB[k] = nB / pb
                eA[k] = fa; eB[k] = fb
                fe = fee * (nA + nB)
                s_eq0[k] = eq; s_cost[k] = fe
                kk = k if sleeve else 0
                cash[kk] -= fe
                fees += fe
                pos[k] = d; ia[k] = a; ib[k] = b; tent[k] = i
                ntr += 1
        # ---- 2. liquidation check at conservative intrabar extremes (mark price)
        anypos = False
        for k in range(K):
            if pos[k] != 0:
                anypos = True
        if anypos:
            bars_in += 1
        worst_tot = 0.0
        if sleeve:
            for k in range(K):
                if pos[k] == 0:
                    worst_tot += cash[k]
                    continue
                a = ia[k]; b = ib[k]
                wa = ML[a, i] if qA[k] > 0 else MH[a, i]
                wb = ML[b, i] if qB[k] > 0 else MH[b, i]
                if np.isnan(wa):
                    wa = lastc[a]
                if np.isnan(wb):
                    wb = lastc[b]
                ew = cash[k] + qA[k] * (wa - eA[k]) + qB[k] * (wb - eB[k])
                mm = abs(qA[k]) * wa * (mmr[a] + fee) + abs(qB[k]) * wb * (mmr[b] + fee)
                if ew <= mm:
                    nliq += 1
                    if nrec < rec:
                        TR[nrec, 0] = k; TR[nrec, 1] = a; TR[nrec, 2] = b; TR[nrec, 3] = tent[k]; TR[nrec, 4] = i
                        TR[nrec, 5] = -1.0
                        TR[nrec, 6] = 5; TR[nrec, 7] = pos[k]; TR[nrec, 8] = s_eq0[k]
                        nrec += 1
                    cash[k] = 0.0
                    pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1
                    blocked[k] = 1
                    ew = 0.0
                worst_tot += ew
        else:
            ew = cash[0]; mm = 0.0
            for k in range(K):
                if pos[k] == 0:
                    continue
                a = ia[k]; b = ib[k]
                wa = ML[a, i] if qA[k] > 0 else MH[a, i]
                wb = ML[b, i] if qB[k] > 0 else MH[b, i]
                if np.isnan(wa):
                    wa = lastc[a]
                if np.isnan(wb):
                    wb = lastc[b]
                ew += qA[k] * (wa - eA[k]) + qB[k] * (wb - eB[k])
                mm += abs(qA[k]) * wa * (mmr[a] + fee) + abs(qB[k]) * wb * (mmr[b] + fee)
            if anypos and ew <= mm:
                nliq += 1
                if nrec < rec:
                    TR[nrec, 0] = -1; TR[nrec, 4] = i; TR[nrec, 5] = -1.0; TR[nrec, 6] = 5
                    nrec += 1
                dead = True
                cash[0] = 0.0
                for k in range(K):
                    pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1
                ew = 0.0
            worst_tot = ew
        if peak > 0:
            dd = 1.0 - worst_tot / peak
            if dd > maxdd:
                maxdd = dd
        # ---- 3. funding at the bar close
        for k in range(K):
            if pos[k] != 0:
                kk = k if sleeve else 0
                fa = FR[ia[k], i]; fb = FR[ib[k], i]
                if fa != 0.0 and not np.isnan(C[ia[k], i]):
                    x = qA[k] * C[ia[k], i] * fa
                    cash[kk] -= x; fund += x; s_cost[k] += x
                if fb != 0.0 and not np.isnan(C[ib[k], i]):
                    x = qB[k] * C[ib[k], i] * fb
                    cash[kk] -= x; fund += x; s_cost[k] += x
        # ---- 4. close equity
        eq = 0.0; gross = 0.0
        for k in range(K):
            if sleeve or k == 0:
                eq += cash[k]
            if pos[k] != 0:
                eq += qA[k] * (lastc[ia[k]] - eA[k]) + qB[k] * (lastc[ib[k]] - eB[k])
                gross += abs(qA[k]) * lastc[ia[k]] + abs(qB[k]) * lastc[ib[k]]
        if eq > 0:
            grosssum += gross / eq
        if eq > peak:
            peak = eq
        dd = 1.0 - eq / peak
        if dd > maxdd:
            maxdd = dd
        daily[DAY[i]] = eq
        if dead:
            continue
        if not sleeve and eq <= 0:
            dead = True
            continue
        # ---- 5. signals at the close -> orders for the next open
        last = (i == i1 - 1)
        for k in range(K):
            z = Z[k, i]
            if pos[k] != 0:
                ex = 0
                if last:
                    ex = 4
                elif not np.isnan(z):
                    if pos[k] == 1:
                        if z <= -zstop:
                            ex = 2
                        elif z >= -zout:
                            ex = 1
                    else:
                        if z >= zstop:
                            ex = 2
                        elif z <= zout:
                            ex = 1
                if ex == 0 and i - tent[k] >= maxhold:
                    ex = 3
                if ex != 0:
                    pend[k] = 2
                    s_why[k] = ex
                    if ex == 2:
                        nstop += 1; blocked[k] = 1
                    if ex == 3:
                        ntime += 1
            elif not last and blocked[k] == 0 and PA[k, i] >= 0 and not np.isnan(z):
                if PA[k, i + 1] == PA[k, i] and PB[k, i + 1] == PB[k, i]:
                    if z <= -zin and z > -zstop:
                        pend[k] = 1
                    elif z >= zin and z < zstop:
                        pend[k] = -1
    # close everything at the end at the final close (taker + slippage), already requested via ex=4 but the
    # next open is outside the window: settle at the last close here
    eq = 0.0
    for k in range(K):
        if sleeve or k == 0:
            eq += cash[k]
        if pos[k] != 0 and not dead:
            sa = SA[k, i1 - 1] + 0.0; sb = SB[k, i1 - 1]
            fa = lastc[ia[k]] * (1.0 - sa) if qA[k] > 0 else lastc[ia[k]] * (1.0 + sa)
            fb = lastc[ib[k]] * (1.0 - sb) if qB[k] > 0 else lastc[ib[k]] * (1.0 + sb)
            fe = fee * (abs(qA[k]) * fa + abs(qB[k]) * fb)
            eq += qA[k] * (fa - eA[k]) + qB[k] * (fb - eB[k]) - fe
            fees += fe
    if dead:
        eq = 0.0
    daily[DAY[i1 - 1]] = eq
    stats = np.array([eq, maxdd, ntr, nliq, nstop, ntime, nforce, fees, fund, bars_in / (i1 - i0),
                      grosssum / (i1 - i0)])
    return daily, stats, TR[:nrec]


# ------------------------------------------------------------------ metrics
def metrics(daily, days):
    e = pd.Series(daily, index=days).ffill().fillna(1.0)
    e0 = pd.concat([pd.Series([1.0], index=[days[0] - pd.Timedelta(days=1)]), e])
    r = e0.pct_change().dropna().replace([np.inf, -np.inf], np.nan).fillna(-1.0)
    yrs = len(days) / 365.25
    fin = e.iloc[-1]
    cagr = fin ** (1 / yrs) - 1 if fin > 0 else -1.0
    sh = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else 0.0
    py = {}
    for y in sorted(set(days.year)):
        ey = e0[(e0.index.year == y)]
        prev = e0[e0.index < pd.Timestamp(f'{y}-01-01')]
        base = prev.iloc[-1] if len(prev) else 1.0
        py[y] = (ey.iloc[-1] / base - 1) if base > 0 else -1.0
    return dict(final=fin, cagr=cagr, sharpe=sh, worst_day=r.min(), per_year=py)


# ------------------------------------------------------------------ driver
def prepare():
    P = load_panel()
    syms = list(P['syms'])
    P['logc'] = np.log(P['c'].astype(np.float64))
    first_bar = np.array([np.argmax(~np.isnan(P['c'][j])) if (~np.isnan(P['c'][j])).any() else NB
                          for j in range(len(syms))])
    U = monthly_universe(syms)
    mmr, imr = load_tiers(syms)
    return P, syms, first_bar, U, mmr, imr


def run_grid(P, syms, U, sel, mmr, imr, schemes, sig_grid, levs, margins, periods, tag, rec=0, fee=FEE_T, slip_mult=1.0):
    O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
    Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
    FR = P['fr'].astype(np.float64)
    day_all = GRID.normalize()
    rows = []
    t_start = time.time()
    for (method, hedge, Wf, K) in schemes:
        for Wz in sorted(set(s[0] for s in sig_grid)):
            PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[(method, hedge, Wf)], K, Wz, base_slip)
            SA = SA * slip_mult; SB = SB * slip_mult
            for (wz, zin, zout, zstop) in [s for s in sig_grid if s[0] == Wz]:
                for pname, (p0, p1) in periods.items():
                    i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
                    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
                    DAY = ((day_all - p0).days).values.astype(np.int64)
                    for margin in margins:
                        for lev in levs:
                            if margin == 'sleeve' and lev < 5:
                                continue      # identical to cross when no liquidation is possible
                            daily, st, _ = sim(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z,
                                            SA, SB, NEWM, mmr, imr, zin, zout, zstop, Wz, float(lev),
                                            margin == 'sleeve', fee, RANGE_SLIP * slip_mult, 1)
                            m = metrics(daily, days)
                            rows.append(dict(method=method, hedge=hedge, Wf=Wf, K=K, Wz=Wz, zin=zin, zout=zout,
                                             zstop=zstop, margin=margin, lev=lev, period=pname,
                                             cagr=m['cagr'], final=m['final'], sharpe=m['sharpe'],
                                             worst_day=m['worst_day'], maxdd_intrabar=st[1], trades=int(st[2]),
                                             liqs=int(st[3]), stops=int(st[4]), timeouts=int(st[5]),
                                             forced=int(st[6]), fees=st[7], funding=st[8], exposure=st[9],
                                             avg_gross_lev=st[10],
                                             per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()})))
        print(tag, method, hedge, Wf, K, 'done', len(rows), f'{time.time() - t_start:.0f}s', flush=True)
    return pd.DataFrame(rows)


if __name__ == '__main__':
    P, syms, first_bar, U, mmr, imr = prepare()
    sel = build_selections(P, U, first_bar)
    schemes = []
    for method in ['coint', 'corr']:
        for hedge in ['lvl', 'ret']:
            for Wf in [60, 120]:
                for K in [3, 5, 10]:
                    schemes.append((method, hedge, Wf, K))
    for hedge in ['lvl', 'ret']:
        for Wf in [60, 120]:
            schemes.append(('fixed', hedge, Wf, 5))
    sig = []
    for Wz in [72, 168, 336]:
        for zin in [1.5, 2.0, 2.5, 3.0]:
            for zout in [0.0, 0.5]:
                for dz in [1.5, 3.0, 99.0]:
                    sig.append((Wz, zin, zout, zin + dz))
    periods = {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}
    df = run_grid(P, syms, U, sel, mmr, imr, schemes, sig, LEVS, ['cross', 'sleeve'], periods, 'grid')
    df.to_csv(os.path.join(OUT, 'grid_results.csv'), index=False)
    print(df.shape)
