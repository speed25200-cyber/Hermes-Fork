"""
Spot-perp cash-and-carry (long spot + short USDT-M perp, equal notional) backtest with leverage,
OKX-style joint (multi-currency / portfolio) margin, USDT borrowing, intrabar liquidation checks.

Leverage L := notional of EACH leg / account equity   (gross exposure = 2L).
  L = 1  : spot fully paid with own USDT, perp margined by the spot collateral, no borrowing.
  L > 1  : spot bought with own equity + (L-1)*E borrowed USDT (auto-borrow, multi-currency /
           portfolio margin); perp short margined by the same account (joint margin).

Data: Binance USD-M perp + spot 1h klines, Binance funding history, Binance mark/index/premium
index 1h klines (proxy for OKX), OKX USDT market borrow rate (hourly, public endpoint).
Margin parameters: OKX public API (2026-09): discount rates, liquidation penalty, swap tier-1
MMR/IMR, USDT borrowing MMR 2% / IMR 10%.
"""
import numpy as np, pandas as pd

D = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data'
ALL = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'BNBUSDT', 'ADAUSDT', 'LINKUSDT', 'AVAXUSDT', 'LTCUSDT']
# OKX public API values fetched 2026-09-24 (tier 1, small account)
HAIRCUT = dict(BTCUSDT=.02, ETHUSDT=.02, SOLUSDT=.03, XRPUSDT=.03, DOGEUSDT=.03, BNBUSDT=.05, ADAUSDT=.05, LINKUSDT=.05, AVAXUSDT=.05, LTCUSDT=.05)
LIQPEN = dict(BTCUSDT=.02, ETHUSDT=.02, SOLUSDT=.02, XRPUSDT=.03, DOGEUSDT=.03, BNBUSDT=.03, ADAUSDT=.03, LINKUSDT=.03, AVAXUSDT=.03, LTCUSDT=.03)
PERP_MMR = dict(BTCUSDT=.004, ETHUSDT=.004, SOLUSDT=.004, XRPUSDT=.004, DOGEUSDT=.01, BNBUSDT=.0065, ADAUSDT=.01, LINKUSDT=.0065, AVAXUSDT=.01, LTCUSDT=.0065)
PERP_IMR = dict(BTCUSDT=.01, ETHUSDT=.01, SOLUSDT=.01, XRPUSDT=.01, DOGEUSDT=.02, BNBUSDT=.02, ADAUSDT=.02, LINKUSDT=.02, AVAXUSDT=.02, LTCUSDT=.02)
BORROW_MMR, BORROW_IMR = 0.02, 0.10          # OKX position-tiers MARGIN cross ccy=USDT tier 1
MARGIN_PAIR_MMR = 0.02                          # isolated spot-margin (split-account variant)
FEE_SPOT, FEE_PERP = 0.0010, 0.0005             # OKX VIP0 taker
FEE_SPOT_MK, FEE_PERP_MK = 0.0008, 0.0002       # OKX VIP0 maker
SLIP = dict(BTCUSDT=2e-4, ETHUSDT=2e-4)         # per leg per trade; alts 5 bp
ALT_SLIP = 5e-4

_cache = {}
_m1 = {}


def load_m1():
    if _m1:
        return _m1
    import os
    V = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify'
    z = np.load(f'{V}/m1_aligned.npz')
    miss = np.load(f'{V}/m1_missing.npz')
    P = load(); idx = P['s_c'].index
    off = idx.searchsorted(pd.Timestamp('2022-01-01', tz='UTC'))
    ok = np.zeros(len(idx), bool)
    bad = np.zeros(len(idx) - off, bool)
    for k in miss.files:
        bad |= miss[k].reshape(-1, 60).any(1)
    ok[off:] = ~bad
    for tag in ['mk_h', 'mk_c', 'ix_h', 'ix_c']:
        _m1[tag] = np.stack([z[f'{tag}_BTCUSDT'], z[f'{tag}_ETHUSDT']], axis=2).astype(float)
    _m1['ok'] = ok
    return _m1


def load():
    if _cache:
        return _cache
    P = {}
    for k in ['s_c', 's_h', 's_l', 'f_c', 'f_h', 'p_h', 'p_c', 'm_h', 'm_c', 'i_h', 'i_l', 'i_c', 'fund']:
        P[k] = pd.read_parquet(f'{D}/panel/{k}.parquet')[ALL]
    idx = P['s_c'].index
    P['spot_ok'] = P['s_c'].notna() & (P['s_h'] > P['s_l'])       # Binance spot trading/fresh in this bar
    for k in ['s_c', 'f_c']:
        P[k] = P[k].ffill()
    P['s_h'] = P['s_h'].fillna(P['s_c']); P['s_l'] = P['s_l'].fillna(P['s_c'])
    # mark-premium (what OKX-like margin uses: mark vs index)
    mp_c = (P['m_c'] / P['i_c'] - 1)
    mp_h = (P['m_h'] / P['i_h'] - 1)
    basis_c = P['f_c'] / P['s_c'] - 1
    mp_c = mp_c.fillna(basis_c)
    mp_h = mp_h.fillna(np.maximum(basis_c, P['f_h'] / P['s_h'] - 1))
    P['jump_mark'] = (mp_h - mp_c.shift(1)).clip(lower=0).fillna(0)
    # valuation prices used by the exchange for margin: spot collateral at index, perp at mark
    P['v_s'] = P['i_c'].fillna(P['s_c']).ffill()
    P['v_f'] = P['m_c'].fillna(P['v_s'] * (1 + mp_c)).ffill()
    P['v_sh'] = P['i_h'].fillna(P['s_h']).ffill()
    P['v_sl'] = P['i_l'].fillna(P['s_l']).ffill()
    P['v_fh'] = P['m_h'].fillna(P['f_h']).ffill()
    P['jump_raw'] = (P['p_h'].fillna(P['p_c']) - P['p_c'].shift(1)).clip(lower=0).fillna(0)
    r = pd.read_csv(f'{D}/okx_usdt_lending_rate_hourly.csv', parse_dates=['ts']).set_index('ts')['rate']
    r = r[~r.index.duplicated()].reindex(idx).ffill().bfill()
    P['borrow_okx'] = r
    P['ret_abs'] = (P['s_c'].pct_change().abs()).fillna(0)
    _cache.update(P)
    return P


def sim(coins, L, mode='always', band=0.05, lookback_d=7, theta=0.0, K=2, borrow='okx', stress='mark',
        start='2022-01-01', end='2026-08-31 23:00', maker=False, margin='joint', cooldown_h=24,
        mr_filter=1.2, record=False, intrabar='proxy', okx=None, close_only=False):
    """Run one backtest. Returns dict of metrics (+ series if record)."""
    P = load()
    cols = [ALL.index(c) for c in coins]
    idx = P['s_c'].index
    t0 = idx.searchsorted(pd.Timestamp(start, tz='UTC'))
    t1 = idx.searchsorted(pd.Timestamp(end, tz='UTC'), side='right')
    S = P['v_s'].values[:, cols]; SH = P['v_sh'].values[:, cols]; SL = P['v_sl'].values[:, cols]
    F = P['v_f'].values[:, cols]; FH = P['v_fh'].values[:, cols]
    SX = P['s_c'].values[:, cols]; FX = P['f_c'].values[:, cols]; SOK = P['spot_ok'].values[:, cols]
    FUND = P['fund'].values[:, cols]
    JMP = (P['jump_mark'] if stress == 'mark' else P['jump_raw']).values[:, cols]
    RA = P['ret_abs'].values[:, cols]
    if borrow == 'okx':
        B = P['borrow_okx'].values
    else:
        B = np.full(len(idx), float(borrow))
    n = len(cols)
    h = np.array([HAIRCUT[c] for c in coins]); lp = np.array([LIQPEN[c] for c in coins])
    mm = np.array([PERP_MMR[c] for c in coins]); im = np.array([PERP_IMR[c] for c in coins])
    slip = np.array([SLIP.get(c, ALT_SLIP) for c in coins])
    fs, fp = (FEE_SPOT_MK, FEE_PERP_MK) if maker else (FEE_SPOT, FEE_PERP)
    # per-coin maintainability filter at this leverage (single-coin margin ratio at entry)
    mr_single = (1 - h * L) / (mm * L + BORROW_MMR * max(L - 1, 0))
    allowed = mr_single >= mr_filter
    # trailing funding (annualised) for rotation: rolling sum over lookback of funding events
    if mode == 'rotate':
        fund_df = P['fund'][list(coins)]
        trail = (fund_df.rolling(lookback_d * 24, min_periods=lookback_d * 24).sum() * 365.0 / lookback_d).values
        btrail = pd.Series(B, index=idx).rolling(lookback_d * 24, min_periods=1).mean().values

    E0 = 1.0
    C = E0                 # USDT balance (perp PnL realised hourly into it)
    Q = np.zeros(n)        # coins held spot == coins short perp
    w = np.zeros(n)        # target weights (fraction of L*E)
    if mode == 'always':
        w = np.where(allowed, 1.0 / n, 0.0)
    held = np.zeros(n, bool)
    cool_until = -1
    nT = t1 - t0
    Ec = np.empty(nT); Emin = np.empty(nT); lev = np.empty(nT); MR = np.full(nT, np.nan)
    liq_times = []; trades = 0; fees_tot = 0.0; fund_tot = 0.0; int_tot = 0.0; imr_viol = 0; rebals = 0
    # split-margin state (margin == 'split'): per-coin spot-account equity A and perp-account equity Bm
    A = np.zeros(n); Bm = np.zeros(n)
    E_prev = E0
    dead = False
    if intrabar == '1m':
        assert list(coins) == ['BTCUSDT', 'ETHUSDT']
        M1 = load_m1()
    m1_used = 0
    for k in range(nT):
        t = t0 + k
        C_start = C
        if dead:
            Ec[k] = 0.0; Emin[k] = 0.0; lev[k] = 0.0
            continue
        s, f = S[t], F[t]
        sp, fprev = S[t - 1], F[t - 1]
        N_prev = Q * sp
        # --- accruals over the bar (positions from previous close) ---
        if margin == 'joint':
            liab = max(0.0, -C)
            intr = liab * B[t] / 8760.0
        else:
            intr = float(np.maximum(0.0, Q * sp - A).sum()) * B[t] / 8760.0
        fnd_i = Q * f * FUND[t]
        fnd = float(fnd_i.sum())
        C += fnd - intr
        fund_tot += fnd; int_tot += intr
        pnl_perp = -Q * (f - fprev)
        C += pnl_perp.sum()
        E = C + float(np.dot(Q, s))
        liquidated = False
        if Q.any():
            if margin == 'joint':
                # intrabar worst point: basis jump (mark premium over index) evaluated at the spot high
                if okx is not None and t in okx:
                    m1_used += 1
                    o = okx[t]
                    cand = []
                    for mkx, ixx in ((('mk_c', 'ix_c'),) if close_only else (('mk_c', 'ix_c'), ('mk_h', 'ix_h'))):
                        Nm = o[ixx] * Q
                        Em = E_prev - intr + (Q * ((o[ixx] - o['ix_prev']) - (o[mkx] - o['mk_prev']))).sum(1)
                        liab_m = np.maximum(0.0, Nm.sum(1) - Em)
                        adj_m = Em - (Nm * h).sum(1)
                        mmr_m = (Nm * mm).sum(1) + BORROW_MMR * liab_m
                        cand.append((adj_m / mmr_m, Em, Nm))
                    mrs = np.concatenate([c[0] for c in cand]); j = int(np.argmin(mrs))
                    c_ = cand[j // 60]; Ew = float(c_[1][j % 60]); Nw = c_[2][j % 60]
                    Emin_m1 = float(min(c[1].min() for c in cand))
                elif intrabar == '1m' and M1['ok'][t]:
                    m1_used += 1
                    Cp = C_start - intr
                    cand = []
                    for mkx, ixx in ((('mk_c', 'ix_c'),) if close_only else (('mk_c', 'ix_c'), ('mk_h', 'ix_h'))):
                        Nm = M1[ixx][t] * Q                       # (60, n) notional at index
                        Em = Cp + (Nm - (M1[mkx][t] - fprev) * Q).sum(1)
                        liab_m = np.maximum(0.0, Nm.sum(1) - Em)
                        adj_m = Em - (Nm * h).sum(1)
                        mmr_m = (Nm * mm).sum(1) + BORROW_MMR * liab_m
                        cand.append((adj_m / mmr_m, Em, Nm))
                    mrs = np.concatenate([c[0] for c in cand]); j = int(np.argmin(mrs))
                    c_ = cand[j // 60]; Ew = float(c_[1][j % 60]); Nw = c_[2][j % 60]
                    Emin_m1 = float(min(c[1].min() for c in cand))
                else:
                    Ew = E_prev - float(np.dot(N_prev, JMP[t])) - intr
                    Ew = min(Ew, E)
                    Nw = Q * SH[t]
                    Emin_m1 = None
                liab_w = max(0.0, Nw.sum() - Ew)
                adj = Ew - float(np.dot(h, Nw))
                mmr = float(np.dot(mm, Nw)) + BORROW_MMR * liab_w
                MR[k] = adj / mmr if mmr > 0 else np.nan
                if adj < mmr:
                    liquidated = True
                    # exchange closes everything: spot collateral sold with OKX liquidation penalty,
                    # perp maintenance margin forfeited, taker fee + slippage on the perp close
                    pen = float(np.dot(lp, Nw)) + float(np.dot(mm, Nw)) + float(np.dot(fp + slip, Nw))
                    Eafter = max(0.0, Ew - pen)
                    Emin_k = min(Ew, Eafter)
            else:
                # separate accounts: spot-margin account A (long spot on borrowed USDT), perp account Bm
                ia = np.maximum(0.0, Q * sp - A) * B[t] / 8760.0
                A_w = A - ia + Q * (SL[t] - sp)
                B_w = Bm + fnd_i - Q * (FH[t] - fprev)
                A = A - ia + Q * (s - sp)
                Bm = Bm + fnd_i + pnl_perp
                liq_a = (Q > 0) & (A_w < MARGIN_PAIR_MMR * Q * SL[t])
                liq_b = (Q > 0) & (B_w < mm * Q * FH[t])
                if (liq_a | liq_b).any():
                    liquidated = True
                    # liquidated leg: its remaining margin is lost (isolated liquidation);
                    # surviving leg closed at the bar close with taker fee + slippage
                    keepA = np.where(liq_a, 0.0, np.maximum(A, 0))
                    keepB = np.where(liq_b, 0.0, np.maximum(Bm, 0))
                    fee_close = np.where(liq_a, 0.0, (fs + slip) * Q * s) + np.where(liq_b, 0.0, (fp + slip) * Q * f)
                    # (liquidated leg margin lost; OKX liquidation of isolated positions keeps no residual)
                    Eafter = max(0.0, float((keepA + keepB - fee_close).sum()) + (C + float(np.dot(Q, s)) - float((A + Bm).sum())))
                    Emin_k = min(Eafter, float(np.minimum(A_w + B_w, A + Bm).sum()))
            if liquidated:
                liq_times.append(idx[t])
                C = Eafter; Q[:] = 0; held[:] = False; A[:] = 0; Bm[:] = 0
                E = Eafter
                cool_until = k + cooldown_h
                if E < 1e-4:
                    dead = True
                    Ec[k] = E; Emin[k] = Emin_k; lev[k] = 0.0
                    continue
        if not liquidated:
            if Q.any() and margin == 'joint':
                Emin_k = min(E, Emin_m1) if ((intrabar == '1m' or okx is not None) and Emin_m1 is not None) else min(E, E_prev - float(np.dot(N_prev, JMP[t])) - intr)
            else:
                Emin_k = E
        # --- decisions at close ---
        if mode == 'rotate' and idx[t].hour == 23:
            sc = trail[t]
            hurdle = btrail[t] * max(L - 1.0, 0.0) / L
            net = sc - hurdle
            ok_stay = allowed & (net > 0) & np.isfinite(net)
            ok_enter = allowed & (net > theta) & np.isfinite(net)
            keep = held & ok_stay
            # rank: keep held first (by score), then candidates
            order = np.argsort(-np.nan_to_num(sc, nan=-9))
            sel = np.zeros(n, bool)
            cnt = 0
            for i in order:
                if keep[i] and cnt < K:
                    sel[i] = True; cnt += 1
            for i in order:
                if cnt >= K:
                    break
                if not sel[i] and ok_enter[i]:
                    sel[i] = True; cnt += 1
            w = np.where(sel, 1.0 / K, 0.0)
        did_rebal = False
        if k >= cool_until and E > 0 and SOK[t].all():
            tgtN = w * L * E
            Ncur = Q * s
            need = np.zeros(n, bool)
            for i in range(n):
                if tgtN[i] == 0 and Q[i] != 0:
                    need[i] = True
                elif tgtN[i] > 0 and (Q[i] == 0 or abs(Ncur[i] / tgtN[i] - 1) > band):
                    need[i] = True
            if need.any():
                newQ = np.where(need, tgtN / s, Q)
                dQ = newQ - Q
                sl = slip * np.where(RA[t] > 0.03, 2.0, 1.0)
                sx, fx = SX[t], FX[t]
                cost = float(np.dot(np.abs(dQ) * sx, fs + sl)) + float(np.dot(np.abs(dQ) * fx, fp + sl))
                # buy/sell spot at the traded price; perp change traded at perp last price but valued at mark
                C -= float(np.dot(dQ, sx)) + cost
                C += float(np.dot(dQ, fx - f))
                if margin == 'split':
                    pass
                fees_tot += cost
                trades += 2 * int(need.sum()); rebals += 1
                Q = newQ
                held = Q > 0
                E = C + float(np.dot(Q, s))
                Nn = Q * s
                liab_n = max(0.0, -C)
                adj_n = E - float(np.dot(h, Nn))
                # OKX checks initial margin on orders that ADD exposure/borrowing (reductions always allowed)
                if dQ.max() > 0 and adj_n < float(np.dot(im, Nn)) + BORROW_IMR * liab_n - 1e-12:
                    imr_viol += 1
                if margin == 'split':
                    # allocate equity between spot-margin and perp accounts to equalise liquidation distance
                    for i in range(n):
                        if Q[i] > 0:
                            Ei = E * w[i] / max(w.sum(), 1e-12)
                            Bm[i] = (Ei + (mm[i] - MARGIN_PAIR_MMR) * Nn[i]) / 2.0
                            A[i] = Ei - Bm[i]
                        else:
                            A[i] = Bm[i] = 0
                Emin_k = min(Emin_k, E)
                did_rebal = True
        if margin == 'split' and not did_rebal and idx[t].hour == 0 and Q.any():
            # daily transfer between accounts to re-equalise distances
            Nn = Q * s
            tot = A + Bm
            Bm = (tot + (mm - MARGIN_PAIR_MMR) * Nn) / 2.0
            A = tot - Bm
        Ec[k] = E; Emin[k] = Emin_k; lev[k] = float((Q * s).sum()) / E if E > 0 else 0.0
        E_prev = E
    ts = idx[t0:t1]
    out = metrics(ts, Ec, Emin)
    out.update(liquidations=len(liq_times), trades=trades, rebalances=rebals, imr_violations=imr_viol,
               fees=fees_tot, funding=fund_tot, interest=int_tot, avg_lev=float(np.mean(lev)),
               liq_dates=';'.join(str(x)[:13] for x in liq_times[:20]), m1_used=m1_used)
    if record:
        out['series'] = pd.DataFrame({'E': Ec, 'Emin': Emin, 'lev': lev, 'mr_worst': MR}, index=ts)
    return out


def metrics(ts, Ec, Emin):
    E = pd.Series(Ec, index=ts)
    Em = pd.Series(Emin, index=ts)
    days = (ts[-1] - ts[0]).total_seconds() / 86400 + 1 / 24
    end = E.iloc[-1]
    cagr = (end / 1.0) ** (365.25 / days) - 1 if end > 0 else -1.0
    peak = np.maximum.accumulate(np.maximum(E.values, 1.0))
    peak_prev = np.concatenate([[1.0], peak[:-1]])
    dd = 1 - np.minimum(Em.values / peak_prev, E.values / peak)
    maxdd = float(np.clip(dd.max(), 0, 1))
    daily = E.resample('D').last()
    daily = pd.concat([pd.Series([1.0], index=[daily.index[0] - pd.Timedelta(days=1)]), daily])
    dr = daily.pct_change().dropna().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = float(dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0.0
    worst_day = float(dr.min()) if len(dr) else 0.0
    yr = {}
    for y in range(ts[0].year, ts[-1].year + 1):
        e = E[str(y)]
        if len(e) == 0:
            continue
        prev = E[:f'{y - 1}-12-31 23:00']
        base = prev.iloc[-1] if len(prev) else 1.0
        yr[y] = (e.iloc[-1] / base - 1) if base > 0 else 0.0
    return dict(cagr=cagr, maxdd=maxdd, sharpe=sharpe, worst_day=worst_day, final=end, years=yr)
