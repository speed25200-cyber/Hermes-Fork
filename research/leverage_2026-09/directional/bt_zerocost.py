"""
Directional high-leverage backtest (BTC / ETH USDT-M perps), 5m execution grid.
Signals on 15m / 1h / 4h bars; stops, take-profits, liquidation and intrabar
equity evaluated on 5m bars (last-price OHLC for stops/TP/equity, mark-price
OHLC for liquidation), funding charged at the real 8h Binance funding events.

Leverage L = position notional / account equity at entry (cross margin, the whole
account backs the single position). Vol-scaled variants use L*min(1, vol_ref/vol_t)
(floor 0.25), i.e. L is the cap.

Protocol: every selection is made on the IS run (2022-01-01..2024-12-31, starts
with equity 1, flat). OOS run (2025-01-01..2026-08-31) restarts with equity 1.
"""
import numpy as np, pandas as pd, json, itertools, time, sys
from numba import njit, prange

ROOT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional'
OUT = ROOT + '/out'

# ---------------- costs (OKX VIP0) ----------------
FEE_T = 0.0     # perp taker
FEE_M = 0.0     # perp maker (take-profit limit orders)
SLIP = 0.0      # 1 bp market-order slippage BTC/ETH
STOP_SLIP = 0.0 # stop-market base slippage 2 bp
STOP_IMPACT = 0.0 # + 10% of the bar's penetration beyond the stop
MMR = 0.004        # OKX tier-1 maintenance margin, BTC/ETH
CAP_FRAC = 0.75    # stop may sit at most at 75% of the distance to the liquidation price
LEVS = [1, 3, 5, 10, 15, 20]

IS0, IS1 = '2022-01-01', '2025-01-01'
OOS0, OOS1 = '2025-01-01', '2026-09-01'

# ---------------- simulator ----------------
@njit(cache=True)
def run_one(o, h, l, c, mo, mh, ml, fund, fflag, day, i0, i1, d0, nd,
            evtL, evtS, exL, exS, stopfrac, volmult, use_vol, L, tp_mult, max_hold,
            conservative, rec_trades):
    E = 1.0
    pos = 0
    Q = 0.0; P0 = 0.0; Ea = 0.0; stop = 0.0; tp = 0.0; mliq = 0.0; Epre = 0.0; ent_i = 0; levE = 0.0
    peak = 1.0; maxdd = 0.0
    daily = np.full(nd, np.nan)
    ntr = 0; nliq = 0; nstop = 0; ntp = 0
    tr_ret = np.zeros(200000); tr_lev = np.zeros(200000)
    exposure = 0; levsum = 0.0
    dead = False
    for i in range(i0, i1):
        if dead:
            daily[day[i] - d0] = E
            continue
        exited = False
        exit_px = 0.0; exit_fee = 0.0; liq = False
        # 1. funding at the open of the bar (positions held into the funding timestamp)
        if pos != 0 and fflag[i] == 1:
            Ea -= pos * fund[i] * Q * mo[i]
            if pos == 1:
                mliq = (Q * P0 - Ea) / (Q * (1.0 - MMR - FEE_T))
            else:
                mliq = (Ea + Q * P0) / (Q * (1.0 + MMR + FEE_T))
        # 2. gap at open: liquidation / stop / tp already through
        if pos == 1:
            if mo[i] <= mliq:
                liq = True
            elif o[i] <= stop:
                exit_px = o[i] * (1.0 - STOP_SLIP); exit_fee = FEE_T; nstop += 1
            elif o[i] >= tp:
                exit_px = tp; exit_fee = FEE_M; ntp += 1
        elif pos == -1:
            if mo[i] >= mliq:
                liq = True
            elif o[i] >= stop:
                exit_px = o[i] * (1.0 + STOP_SLIP); exit_fee = FEE_T; nstop += 1
            elif o[i] <= tp:
                exit_px = tp; exit_fee = FEE_M; ntp += 1
        # 3. signal exits at the open (signals only live on TF boundaries)
        if pos != 0 and (not liq) and exit_px == 0.0:
            sig_exit = False
            if pos == 1 and (exL[i] == 1 or evtS[i] == 1):
                sig_exit = True
            if pos == -1 and (exS[i] == 1 or evtL[i] == 1):
                sig_exit = True
            if max_hold > 0 and (evtL[i] | evtS[i] | exL[i] | exS[i] | (i - ent_i >= max_hold)) and (i - ent_i >= max_hold):
                sig_exit = True
            if sig_exit:
                exit_px = o[i] * (1.0 - SLIP * pos); exit_fee = FEE_T
        if liq:
            E = 0.0; nliq += 1; pos = 0
            if rec_trades and ntr < 200000:
                tr_ret[ntr] = -1.0; tr_lev[ntr] = levE
            ntr += 1
            dead = True
            daily[day[i] - d0] = E
            maxdd = 1.0
            continue
        if exit_px > 0.0:
            E = Ea + pos * Q * (exit_px - P0) - exit_fee * Q * exit_px
            if E < 0.0: E = 0.0
            if rec_trades and ntr < 200000:
                tr_ret[ntr] = E / Epre - 1.0; tr_lev[ntr] = levE
            ntr += 1
            pos = 0
            exit_px = 0.0
            if E <= 1e-9:
                dead = True; maxdd = 1.0
                daily[day[i] - d0] = E
                continue
        # 4. entries at the open
        if pos == 0 and (evtL[i] == 1 or evtS[i] == 1):
            d = 1 if evtL[i] == 1 else -1
            levE = L * (volmult[i] if use_vol else 1.0)
            N = levE * E
            P0 = o[i] * (1.0 + SLIP * d)
            Q = N / P0
            Epre = E
            Ea = E - FEE_T * N
            ent_i = i
            pos = d
            if d == 1:
                mliq = (Q * P0 - Ea) / (Q * (1.0 - MMR - FEE_T))
                liqd = (P0 - mliq) / P0 if mliq > 0 else 1.0
            else:
                mliq = (Ea + Q * P0) / (Q * (1.0 + MMR + FEE_T))
                liqd = (mliq - P0) / P0
            sd = stopfrac[i]
            if sd > CAP_FRAC * liqd:
                sd = CAP_FRAC * liqd
            if d == 1:
                stop = P0 * (1.0 - sd)
                tp = P0 * (1.0 + tp_mult * sd) if tp_mult > 0 else 1e18
            else:
                stop = P0 * (1.0 + sd)
                tp = P0 * (1.0 - tp_mult * sd) if tp_mult > 0 else -1.0
        # 5. intrabar: liquidation / stop / tp
        trough = E
        if pos == 1:
            liq_hit = ml[i] <= mliq
            stop_hit = l[i] <= stop
            tp_hit = h[i] > tp
            if liq_hit and (conservative or not stop_hit):
                liq = True
            elif stop_hit:
                pen = (stop - l[i]) / stop
                exit_px = stop * (1.0 - STOP_SLIP - STOP_IMPACT * pen); exit_fee = FEE_T; nstop += 1
            elif tp_hit:
                exit_px = tp; exit_fee = FEE_M; ntp += 1
            if not liq and exit_px == 0.0:
                trough = Ea + Q * (l[i] - P0)
        elif pos == -1:
            liq_hit = mh[i] >= mliq
            stop_hit = h[i] >= stop
            tp_hit = l[i] < tp
            if liq_hit and (conservative or not stop_hit):
                liq = True
            elif stop_hit:
                pen = (h[i] - stop) / stop
                exit_px = stop * (1.0 + STOP_SLIP + STOP_IMPACT * pen); exit_fee = FEE_T; nstop += 1
            elif tp_hit:
                exit_px = tp; exit_fee = FEE_M; ntp += 1
            if not liq and exit_px == 0.0:
                trough = Ea - Q * (h[i] - P0)
        if liq:
            E = 0.0; nliq += 1; pos = 0
            if rec_trades and ntr < 200000:
                tr_ret[ntr] = -1.0; tr_lev[ntr] = levE
            ntr += 1
            dead = True; maxdd = 1.0
            daily[day[i] - d0] = E
            continue
        if exit_px > 0.0:
            E = Ea + pos * Q * (exit_px - P0) - exit_fee * Q * exit_px
            if E < 0.0: E = 0.0
            if rec_trades and ntr < 200000:
                tr_ret[ntr] = E / Epre - 1.0; tr_lev[ntr] = levE
            ntr += 1
            pos = 0
            trough = E
            if E <= 1e-9:
                dead = True; maxdd = 1.0
                daily[day[i] - d0] = E
                continue
        # 6. close: mark-to-market with last price
        if pos != 0:
            Ec = Ea + pos * Q * (c[i] - P0)
            exposure += 1; levsum += levE
        else:
            Ec = E
        if peak > 0:
            dd = 1.0 - trough / peak
            if dd > maxdd: maxdd = dd
            dd = 1.0 - Ec / peak
            if dd > maxdd: maxdd = dd
        if Ec > peak: peak = Ec
        daily[day[i] - d0] = Ec
    # close any open position at window end (taker)
    if pos != 0 and not dead:
        px = c[i1 - 1] * (1.0 - SLIP * pos)
        E = Ea + pos * Q * (px - P0) - FEE_T * Q * px
        if E < 0: E = 0.0
        if rec_trades and ntr < 200000:
            tr_ret[ntr] = E / Epre - 1.0; tr_lev[ntr] = levE
        ntr += 1
        daily[nd - 1] = E
    avg_lev = levsum / exposure if exposure > 0 else 0.0
    expo = exposure / (i1 - i0)
    m = min(ntr, 200000)
    return daily, maxdd, ntr, nliq, nstop, ntp, avg_lev, expo, tr_ret[:m], tr_lev[:m]


@njit(parallel=True, cache=True)
def run_many(o, h, l, c, mo, mh, ml, fund, fflag, day, i0, i1, d0, nd,
             EVL, EVS, EXL, EXS, STOPS, volmult, sig_id, stop_id, use_vol, Ls, tp_mult, max_hold, conservative):
    R = len(sig_id)
    daily = np.zeros((R, nd)); stats = np.zeros((R, 7))
    for r in prange(R):
        s = sig_id[r]
        res = run_one(o, h, l, c, mo, mh, ml, fund, fflag, day, i0, i1, d0, nd,
                      EVL[s], EVS[s], EXL[s], EXS[s], STOPS[stop_id[r]], volmult, use_vol[r] == 1, Ls[r],
                      tp_mult[r], max_hold[r], conservative, False)
        daily[r] = res[0]
        stats[r, 0] = res[1]; stats[r, 1] = res[2]; stats[r, 2] = res[3]; stats[r, 3] = res[4]
        stats[r, 4] = res[5]; stats[r, 5] = res[6]; stats[r, 6] = res[7]
    return daily, stats

# ---------------- data & signals ----------------
TF_BARS = {'15m': 3, '1h': 12, '4h': 48}

def load(sym):
    d = pd.read_parquet(f'{ROOT}/{sym}_5m.parquet')
    return d

def tf_frame(d, m):
    n = len(d) // m * m
    g = np.arange(n) // m
    df = pd.DataFrame({'o': d.o.values[:n][::m], 'h': pd.Series(d.h.values[:n]).groupby(g).max().values,
                       'l': pd.Series(d.l.values[:n]).groupby(g).min().values, 'c': d.c.values[:n][m - 1::m]})
    return df

def to_grid(vals, m, n5, fill=0):
    """TF bar k (closing at 5m index (k+1)m-1) -> action index (k+1)m on the 5m grid."""
    out = np.full(n5, fill, dtype=vals.dtype)
    idx = (np.arange(len(vals)) + 1) * m
    ok = idx < n5
    out[idx[ok]] = vals[ok]
    return out

def to_grid_ffill(vals, m, n5):
    s = np.full(n5, np.nan)
    idx = (np.arange(len(vals)) + 1) * m
    ok = idx < n5
    s[idx[ok]] = vals[ok]
    return pd.Series(s).ffill().values

def signals(d, tf, fam, p):
    m = TF_BARS[tf]; n5 = len(d)
    b = tf_frame(d, m)
    c = b.c
    if fam == 'ema':
        f, s = p
        diff = c.ewm(span=f, adjust=False).mean() - c.ewm(span=s, adjust=False).mean()
        up = (diff > 0); dn = (diff < 0)
        evL = up & ~up.shift(1, fill_value=False)
        evS = dn & ~dn.shift(1, fill_value=False)
        exL = dn; exS = up; mh = 0
    elif fam == 'donch':
        N = p; N2 = N // 2
        hiN = b.h.rolling(N).max().shift(1); loN = b.l.rolling(N).min().shift(1)
        hi2 = b.h.rolling(N2).max().shift(1); lo2 = b.l.rolling(N2).min().shift(1)
        evL = c > hiN; evS = c < loN; exL = c < lo2; exS = c > hi2; mh = 0
    elif fam == 'mr':
        k = p; LB = 20
        mu = c.rolling(LB).mean(); sd = c.rolling(LB).std()
        z = (c - mu) / sd
        evL = (z < -k) & ~(z.shift(1) < -k)
        evS = (z > k) & ~(z.shift(1) > k)
        exL = z >= 0; exS = z <= 0; mh = 2 * LB * m   # time stop: 40 TF bars
    # ATR14 on TF bars (for ATR stops)
    tr = pd.concat([b.h - b.l, (b.h - c.shift()).abs(), (b.l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean() / c
    g = lambda x: to_grid(x.fillna(False).values.astype(np.int8), m, n5)
    return g(evL), g(evS), g(exL), g(exS), to_grid_ffill(atr.values, m, n5), mh

def vol_mult(d, is0, is1):
    m = 12
    b = tf_frame(d, m)
    r = np.log(b.c).diff()
    v = r.rolling(168).std()
    vg = to_grid_ffill(v.values, m, len(d))
    t = d.t.values
    msk = (t >= is0) & (t < is1)
    ref = np.nanmedian(vg[msk])
    vm = np.clip(ref / vg, 0.25, 1.0)
    vm = np.nan_to_num(vm, nan=1.0)
    return vm, ref

SIG_GRID = []
for tf in ['15m', '1h', '4h']:
    for p in [(20, 100), (50, 200)]: SIG_GRID.append((tf, 'ema', p))
    for p in [20, 55]: SIG_GRID.append((tf, 'donch', p))
    for p in [2.0, 3.0]: SIG_GRID.append((tf, 'mr', p))
STOP_KINDS = ['fix1.5%', 'atr2x']
TP_KINDS = [0.0, 2.0]
SIZING = ['fixed', 'volscaled']

def ms(x): return int(pd.Timestamp(x).value // 10**6)

def metrics(daily, t_days, start_eq=1.0):
    eq = np.concatenate([[start_eq], daily])
    eq = pd.Series(eq)
    rets = eq.pct_change().iloc[1:].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    days = len(daily)
    fin = daily[-1]
    cagr = fin ** (365.0 / days) - 1 if fin > 0 else -1.0
    sd = rets.std()
    sharpe = rets.mean() / sd * np.sqrt(365) if sd > 0 else 0.0
    worst = rets.min()
    yrs = {}
    ys = pd.Series(daily, index=t_days)
    prev = start_eq
    for y in sorted(set(t_days.year)):
        last = ys[ys.index.year == y].iloc[-1]
        yrs[y] = (last / prev - 1) if prev > 0 else -1.0
        prev = last
    return cagr, sharpe, worst, yrs, fin

def main(conservative=True, tag='cons'):
    rows = []
    trade_cache = {}
    for sym in ['BTCUSDT', 'ETHUSDT']:
        d = load(sym)
        n5 = len(d)
        t = d.t.values
        day = ((t - t[0]) // 86400000).astype(np.int64)
        arrs = [d[k].values.astype(np.float64) for k in ['o', 'h', 'l', 'c', 'mo', 'mh', 'ml', 'fund']]
        fflag = d.fund_flag.values.astype(np.int8)
        vm, vref = vol_mult(d, ms(IS0), ms(IS1))
        EVL, EVS, EXL, EXS, ATRS, MH = [], [], [], [], {}, []
        for (tf, fam, p) in SIG_GRID:
            a, b_, c_, d_, atr, mh = signals(d, tf, fam, p)
            EVL.append(a); EVS.append(b_); EXL.append(c_); EXS.append(d_); MH.append(mh); ATRS[tf] = atr
        EVL = np.array(EVL); EVS = np.array(EVS); EXL = np.array(EXL); EXS = np.array(EXS)
        STOPS = np.array([np.full(n5, 0.015), 2 * ATRS['15m'], 2 * ATRS['1h'], 2 * ATRS['4h']])
        STOPS = np.nan_to_num(STOPS, nan=0.015)
        tf_stop_idx = {'15m': 1, '1h': 2, '4h': 3}
        # run list
        runs = []
        for si, (tf, fam, p) in enumerate(SIG_GRID):
            for sk in STOP_KINDS:
                for tpm in TP_KINDS:
                    for sz in SIZING:
                        for L in LEVS:
                            runs.append(dict(sym=sym, tf=tf, fam=fam, param=str(p), stop=sk, tp=tpm, sizing=sz, L=L,
                                             sig_id=si, stop_id=0 if sk == 'fix1.5%' else tf_stop_idx[tf],
                                             use_vol=1 if sz == 'volscaled' else 0, mh=MH[si]))
        R = pd.DataFrame(runs)
        for win, (a0, a1) in {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}.items():
            i0 = int(np.searchsorted(t, ms(a0))); i1 = int(np.searchsorted(t, ms(a1)))
            d0 = int(day[i0]); nd = int(day[i1 - 1] - d0 + 1)
            t_days = pd.to_datetime(t[0], unit='ms') + pd.to_timedelta(np.arange(d0, d0 + nd), unit='D')
            t_days = pd.DatetimeIndex(t_days)
            tt = time.time()
            daily, st = run_many(*arrs, fflag, day, i0, i1, d0, nd, EVL, EVS, EXL, EXS, STOPS, vm,
                                 R.sig_id.values.astype(np.int64), R.stop_id.values.astype(np.int64),
                                 R.use_vol.values.astype(np.int64), R.L.values.astype(np.float64),
                                 R.tp.values.astype(np.float64), R.mh.values.astype(np.int64), conservative)
            print(sym, win, tag, 'runs', len(R), 'sec', round(time.time() - tt, 1), flush=True)
            for r in range(len(R)):
                cagr, sh, worst, yrs, fin = metrics(daily[r], t_days)
                R.loc[r, f'{win}_cagr'] = cagr; R.loc[r, f'{win}_sharpe'] = sh; R.loc[r, f'{win}_worstday'] = worst
                R.loc[r, f'{win}_final'] = fin; R.loc[r, f'{win}_maxdd'] = st[r, 0]; R.loc[r, f'{win}_trades'] = st[r, 1]
                R.loc[r, f'{win}_liq'] = st[r, 2]; R.loc[r, f'{win}_stops'] = st[r, 3]; R.loc[r, f'{win}_tps'] = st[r, 4]
                R.loc[r, f'{win}_avglev'] = st[r, 5]; R.loc[r, f'{win}_exposure'] = st[r, 6]
                for y, v in yrs.items(): R.loc[r, f'y{y}'] = v
        R['vol_ref_1h'] = vref
        rows.append(R)
    res = pd.concat(rows, ignore_index=True)
    res['config'] = res.sym + '|' + res.tf + '|' + res.fam + '|' + res.param + '|' + res.stop + '|tp' + res.tp.astype(str) + '|' + res.sizing
    res.to_csv(f'{OUT}/grid_results_{tag}_ZEROCOST.csv', index=False)
    return res

if __name__ == '__main__':
    tag = sys.argv[1] if len(sys.argv) > 1 else 'cons'
    main(conservative=(tag == 'cons'), tag=tag)
