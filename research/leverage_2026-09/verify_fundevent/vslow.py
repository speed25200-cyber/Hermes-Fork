"""Family (iii): hold the funding-RECEIVING side while funding stays extreme (1h bars).

Decisions only at a coin's own settlement times t, right after the settlement, using the rate realised AT t
(published at t, so known): open when the annualised rate |ann_t| >= thr_in (side = -sign(ann_t)),
close at a later settlement of the coin when the received annualised rate (-side*ann_t) < thr_out
(thr_out = 0 or 0.5*thr_in).  Fills at the open of the 1h bar starting at t (taker + slippage).
Hedge: 'btc' (opposite BTCUSDT perp, beta*notional, beta = 7d OLS on 1h returns known at t-1h, quantities fixed
       during the hold), 'none', or 'spot' (positive funding only: short perp + long Binance spot bought with
       USDT borrowed at the OKX hourly USDT lending rate; spot counted at full value as collateral and no
       maintenance margin on the loan -> optimistic, OKX haircuts alt collateral).
Slots: at most K pairs; a new pair gets margin min(cash, total equity / K), in its own cross-margin sub-account
       with coin notional = L * margin (L = leverage).  Liquidation: in each 1h bar, coin at its adverse
       extreme AND hedge at its adverse extreme; if the pair equity <= MM (OKX tier-1 MMR of the coin, 2.5%
       default, + 0.4% of the BTC hedge) the pair's margin is lost; the rest of the account continues.
Funding: Binance realised rate at every settlement of the coin while held; BTC funding on the hedge.
IS 2022-01-01..2024-12-31 and OOS 2025-01-01..2026-08-31 are separate runs starting flat with equity 1.
"""
import os, sys, json, itertools, time
import numpy as np, pandas as pd
from multiprocessing import Pool
from common import D   # verifier: points to /dev/shm/fundevent (read-only)

FEE_T, FEE_SPOT = 5e-4, 1e-3
BTC_SLIP0, BTC_MMR, RANGE_K = 1e-4, 0.004, 0.10
H0 = pd.Timestamp('2021-12-01').value // 10 ** 6
IS0, IS1, OOS1 = pd.Timestamp('2022-01-01'), pd.Timestamp('2025-01-01'), pd.Timestamp('2026-09-01')
LEVS = [1, 3, 5, 10, 15, 20]
G = {}


def hidx(ts):
    return ((np.asarray(ts, dtype=np.int64) - H0) // 3600000).astype(np.int64)


def build():
    p = os.path.join(D, 'slow_arrays.npz')
    st = pd.read_parquet(os.path.join(D, 'settle.parquet'))
    k = pd.read_parquet(os.path.join(D, 'k1h.parquet'))
    ks = pd.read_parquet(os.path.join(D, 'k1h_spot.parquet')) if os.path.exists(os.path.join(D, 'k1h_spot.parquet')) else None
    syms = sorted(set(k.sym) - {'BTCUSDT', 'ETHUSDT'})
    si = {s: i for i, s in enumerate(syms)}
    NH = int(hidx([OOS1.value // 10 ** 6])[0]) + 1
    NS = len(syms)
    A = {c: np.full((NS, NH), np.nan, np.float32) for c in ['o', 'h', 'l', 'c', 'v24', 'beta', 'rate', 'ann']}
    btc = k[k.sym == 'BTCUSDT'].copy()
    bh = hidx(btc.t)
    okb = (bh >= 0) & (bh < NH)
    B = {c: np.full(NH, np.nan) for c in ['o', 'h', 'l', 'c', 'rate']}
    for c in ['o', 'h', 'l', 'c']:
        B[c][bh[okb]] = btc[c].values[okb]
    rb = pd.Series(np.log(B['c'])).diff()
    kk = k[k.sym.isin(si)]
    for sym, g in kk.groupby('sym'):
        i = si[sym]
        hh = hidx(g.t)
        ok = (hh >= 0) & (hh < NH)
        for c in ['o', 'h', 'l', 'c']:
            A[c][i, hh[ok]] = g[c].values[ok]
        full_c = pd.Series(A['c'][i].astype(np.float64))
        r = np.log(full_c).diff()
        okr = r.notna() & rb.notna()
        x, y = rb.where(okr), r.where(okr)
        n = okr.astype(float).rolling(168, min_periods=1).sum()
        mx = x.rolling(168, min_periods=72).mean(); my = y.rolling(168, min_periods=72).mean()
        cov = (x * y).rolling(168, min_periods=72).mean() - mx * my
        var = (x * x).rolling(168, min_periods=72).mean() - mx * mx
        beta = (cov / var).where(n >= 72)
        qv = np.full(NH, np.nan); qv[hh[ok]] = g.qv.values[ok]
        v24 = pd.Series(qv).rolling(24, min_periods=12).sum()
        # value usable at hour h = computed through bar h-2 (closed at h-1)
        A['beta'][i, 2:] = beta.values[:-2]
        A['v24'][i, 2:] = v24.values[:-2]
    stt = st[st.sym.isin(si)]
    hh = hidx(stt.t)
    ok = (hh >= 0) & (hh < NH) & (stt.t.values % 3600000 == 0)
    ii = stt.sym.map(si).values
    A['rate'][ii[ok], hh[ok]] = stt.rate.values[ok]
    A['ann'][ii[ok], hh[ok]] = stt.ann.values[ok]
    sb = st[st.sym == 'BTCUSDT']
    hb = hidx(sb.t); okb = (hb >= 0) & (hb < NH)
    B['rate'][hb[okb]] = sb.rate.values[okb]
    S = {c: np.full((NS, NH), np.nan, np.float32) for c in ['o', 'h', 'l', 'c']}
    if ks is not None:
        for sym, g in ks[ks.sym.isin(si)].groupby('sym'):
            i = si[sym]
            hh = hidx(g.t); ok = (hh >= 0) & (hh < NH)
            for c in ['o', 'h', 'l', 'c']:
                S[c][i, hh[ok]] = g[c].values[ok]
    # OKX tiers + point-in-time listing (same sources as prep.py)
    ev = pd.read_parquet(os.path.join(D, 'ev.parquet'), columns=['sym', 't', 'mmr', 'maxlev', 'okx_pit', 'coin'])
    tiers = ev.drop_duplicates('sym').set_index('sym')
    mmr = np.array([tiers.mmr.get(s, 0.025) for s in syms]); maxlev = np.array([tiers.maxlev.get(s, 20.0) for s in syms])
    ND = NH // 24 + 1
    pit = np.zeros((NS, ND), bool)
    e2 = ev[ev.okx_pit & ev.sym.isin(si)]
    d2 = ((e2.t.values - H0) // 86400000).astype(int)
    okd = (d2 >= 0) & (d2 < ND)
    pit[e2.sym.map(si).values.astype(int)[okd], d2[okd]] = True
    lend = pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data/okx_usdt_lending_rate_hourly.csv')
    lh = hidx(((pd.to_datetime(lend.ts, utc=True) - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).values)
    LR = np.full(NH, np.nan)
    okl = (lh >= 0) & (lh < NH)
    LR[lh[okl]] = lend.rate.values[okl]
    LR = pd.Series(LR).ffill().bfill().values          # annual rate (fraction)
    np.savez(p, syms=np.array(syms), mmr=mmr, maxlev=maxlev, pit=pit, LR=LR,
             **{'A_' + c: v for c, v in A.items()}, **{'B_' + c: v for c, v in B.items()}, **{'S_' + c: v for c, v in S.items()})
    print('built arrays', NS, NH)


def build_mark():
    z = np.load(os.path.join(D, 'slow_arrays.npz'))
    syms = list(z['syms']); NS, NH = z['A_o'].shape
    si = {s: i for i, s in enumerate(syms)}
    k = pd.read_parquet(os.path.join(D, 'k1h_mark.parquet'))
    M = {c: np.full((NS, NH), np.nan, np.float32) for c in ['h', 'l']}
    for sym, g in k[k.sym.isin(si)].groupby('sym'):
        hh = hidx(g.t); ok = (hh >= 0) & (hh < NH)
        for c in ['h', 'l']:
            M[c][si[sym], hh[ok]] = g[c].values[ok]
    np.savez(os.path.join(D, 'slow_mark.npz'), M_h=M['h'], M_l=M['l'])
    print('mark arrays', np.isfinite(M['h']).mean().round(3), 'vs last', np.isfinite(z['A_h']).mean().round(3))


def load():
    z = np.load(os.path.join(D, 'slow_arrays.npz'))
    for kx in z.files:
        G[kx] = z[kx]
    pm = os.path.join(D, 'slow_mark.npz')
    if os.path.exists(pm):
        zm = np.load(pm)
        # mark extremes where available, else last-price extremes
        G['M_h'] = np.where(np.isfinite(zm['M_h']), zm['M_h'], G['A_h'])
        G['M_l'] = np.where(np.isfinite(zm['M_l']), zm['M_l'], G['A_l'])
    NS, NH = G['A_o'].shape
    # last hour with data per symbol (delisting)
    ok = np.isfinite(G['A_o'])
    G['last_h'] = np.where(ok.any(axis=1), NH - 1 - np.argmax(ok[:, ::-1], axis=1), -1)
    # candidate lists per hour: symbols with a settlement and |ann| >= 0.5, sorted by |ann| desc
    ann = G['A_ann']
    hs, ss = np.nonzero(np.isfinite(ann).T & (np.abs(np.nan_to_num(ann.T)) >= 0.5))
    cand = {}
    for h, s in zip(hs, ss):
        cand.setdefault(h, []).append(s)
    for h in cand:
        cand[h].sort(key=lambda s: -abs(ann[s, h]))
    G['cand'] = cand
    # settlement lists per hour for held positions
    G['settle_h'] = np.isfinite(G['A_rate'])


def slip_coin(i, h):
    v = G['A_v24'][i, h]
    v = 0.0 if not np.isfinite(v) else v
    s0 = 3e-4 if v >= 2e8 else 5e-4 if v >= 5e7 else 8e-4 if v >= 1e7 else 15e-4
    o, hi, lo = G['A_o'][i, h], G['A_h'][i, h], G['A_l'][i, h]
    rng = (hi - lo) / o if np.isfinite(hi) and o > 0 else 0.0
    return s0 + RANGE_K * rng / np.sqrt(60.0)


def slip_btc(h):
    o, hi, lo = G['B_o'][h], G['B_h'][h], G['B_l'][h]
    return BTC_SLIP0 + RANGE_K * ((hi - lo) / o) / np.sqrt(60.0)


def simulate(cfg, L, t0, t1):
    thr_in, thr_out_f, K, hedge, sidef, universe = cfg
    thr_out = thr_out_f * thr_in
    Ao, Ah, Al, Ac = G['A_o'], G['A_h'], G['A_l'], G['A_c']
    rate, ann, beta_a = G['A_rate'], G['A_ann'], G['A_beta']
    Bo, Bh, Bl, Bc, Br = G['B_o'], G['B_h'], G['B_l'], G['B_c'], G['B_rate']
    So, Sh, Sl, Sc = G['S_o'], G['S_h'], G['S_l'], G['S_c']
    mmr, maxlev, pit, LR, last_h = G['mmr'], G['maxlev'], G['pit'], G['LR'], G['last_h']
    h0 = int(hidx([t0.value // 10 ** 6])[0]); h1 = int(hidx([t1.value // 10 ** 6])[0])
    cash = 1.0
    pos = {}   # sym -> dict
    eq_close = np.empty(h1 - h0); eq_trough = np.empty(h1 - h0)
    ntr = nliq = 0
    fund_sum = cost_sum = 0.0

    def pair_eq(p, i, P, B, S):
        e = p['m'] + p['s'] * p['qc'] * (P - p['pe'])
        if p['hedge'] == 'btc':
            e -= p['s'] * p['qb'] * (B - p['be'])
        elif p['hedge'] == 'spot':
            e += p['qs'] * (S - p['se'])
        return e

    def close(i, h, P, B, S, extra_slip=1.0):
        nonlocal cash, cost_sum
        p = pos.pop(i)
        e = pair_eq(p, i, P, B, S)
        N = p['qc'] * P
        c = N * (FEE_T + extra_slip * slip_coin(i, h))
        if p['hedge'] == 'btc':
            c += p['qb'] * B * (FEE_T + extra_slip * slip_btc(h))
        elif p['hedge'] == 'spot':
            c += p['qs'] * S * (FEE_SPOT + extra_slip * slip_coin(i, h))
        cost_sum += c
        cash += max(e - c, 0.0)
        if G.get('log_trades') is not None:
            G['log_trades'].append((p['h_open'], h, G['syms'][i], p['s'], p['m0'], max(e - c, 0.0)))

    for k, h in enumerate(range(h0, h1)):
        # 1. settlements at h (positions held through h)
        if pos:
            for i, p in pos.items():
                if np.isfinite(rate[i, h]):
                    P = Ao[i, h] if np.isfinite(Ao[i, h]) else p['last']
                    f = -p['s'] * rate[i, h] * p['qc'] * P * G.get('fund_mult', 1.0)
                    p['m'] += f; fund_sum += f
                if p['hedge'] == 'btc' and np.isfinite(Br[h]):
                    p['m'] += p['s'] * p['qb'] * Br[h] * Bo[h]
                if p['hedge'] == 'spot':
                    p['m'] -= p['loan'] * LR[h] / 8760.0
        # 2. exits
        for i in list(pos.keys()):
            p = pos[i]
            if h > last_h[i]:                      # delisted / data ended: close at the last close, 3x slippage
                hl = last_h[i]
                sl_ = Sc[i, hl] if (p['hedge'] == 'spot' and np.isfinite(Sc[i, hl])) else p.get('last_s', np.nan)
                close(i, hl, Ac[i, hl], Bc[hl], sl_, extra_slip=3.0)
                continue
            hx = h - G.get('exit_delay', 0)          # VERIFIER: exit decided at settlement hx, filled at open of h
            if np.isfinite(ann[i, hx]) and np.isfinite(Ao[i, h]) and hx > p['h_open']:
                if -p['s'] * ann[i, hx] < thr_out:
                    so = So[i, h] if p['hedge'] == 'spot' else np.nan
                    if p['hedge'] == 'spot' and not np.isfinite(so):
                        so = p['last_s']
                    close(i, h, Ao[i, h], Bo[h], so)
        # 3. entries
        ED = G.get('entry_delay', 0)             # VERIFIER: entry decided at settlement h-ED, filled at open of h
        cl = G['cand'].get(h - ED)
        if cl and G.get('excl'):
            cl = [i for i in cl if i not in G['excl']]
        if cl and len(pos) < K:
            Etot = cash + sum(pair_eq(p, i, (Ao[i, h] if np.isfinite(Ao[i, h]) else p['last']), Bo[h],
                                      (So[i, h] if (p['hedge'] == 'spot' and np.isfinite(So[i, h])) else p.get('last_s', np.nan)))
                              for i, p in pos.items())
            for i in cl:
                if len(pos) >= K:
                    break
                a = ann[i, h - ED]
                if abs(a) < thr_in or i in pos or not np.isfinite(Ao[i, h]) or h >= last_h[i]:
                    continue
                s = -1.0 if a > 0 else 1.0
                if (sidef == 'pos' and a < 0) or (sidef == 'neg' and a > 0):
                    continue
                hg = hedge
                if hedge == 'spot':
                    if s > 0 or not np.isfinite(So[i, h]):
                        continue
                b = beta_a[i, h] if hg == 'btc' else 0.0
                if hg == 'btc':
                    b = 1.0 if not np.isfinite(b) else min(max(b, 0.2), 3.0)
                if L * (1.0 / maxlev[i] + (b / 100.0 if hg == 'btc' else 0.0)) > 1.0 + 1e-9:
                    continue
                if universe == 'okx' and not pit[i, h // 24]:
                    continue
                m = min(cash, Etot / K)
                if m <= 1e-9:
                    break
                N = L * m
                c = N * (FEE_T + slip_coin(i, h))
                p = {'s': s, 'qc': N / Ao[i, h], 'pe': Ao[i, h], 'hedge': hg, 'last': Ao[i, h]}
                if hg == 'btc':
                    p['qb'] = b * N / Bo[h]; p['be'] = Bo[h]
                    c += b * N * (FEE_T + slip_btc(h))
                if hg == 'spot':
                    p['qs'] = N / So[i, h]; p['se'] = So[i, h]; p['last_s'] = So[i, h]
                    p['loan'] = N
                    c += N * (FEE_SPOT + slip_coin(i, h))
                p['m'] = m - c
                p['m0'] = m; p['h_open'] = h
                cost_sum += c
                cash -= m
                pos[i] = p
                ntr += 1
        # 4. intrabar path + liquidation, mark at close
        tro = cash; clo = cash
        for i in list(pos.keys()):
            p = pos[i]
            if not np.isfinite(Ah[i, h]):
                e = max(pair_eq(p, i, p['last'], Bc[h], p.get('last_s', np.nan)), 0.0)
                tro += e; clo += e
                continue
            s = p['s']
            if G.get('use_mark'):
                Padv = G['M_l'][i, h] if s > 0 else G['M_h'][i, h]
            else:
                Padv = Al[i, h] if s > 0 else Ah[i, h]
            Badv = Bh[h] if s > 0 else Bl[h]
            if p['hedge'] == 'spot':
                # spot at the moment of the perp extreme: perp extreme x the less favourable of the open/close
                # spot/perp ratios of the bar (independent extremes would overstate a same-coin spread)
                ro = So[i, h] / Ao[i, h] if np.isfinite(So[i, h]) else np.nan
                rc = Sc[i, h] / Ac[i, h] if np.isfinite(Sc[i, h]) else np.nan
                rr = np.nanmin([ro, rc]) if np.isfinite(ro) or np.isfinite(rc) else p['last_s'] / p['last']
                Sadv = Padv * rr
            else:
                Sadv = np.nan
            ew = pair_eq(p, i, Padv, Badv, Sadv)
            mm = mmr[i] * p['qc'] * Padv + (BTC_MMR * p['qb'] * Badv if p['hedge'] == 'btc' else 0.0)
            # also check at the bar close (spot hedge: the close basis can be worse than the modelled extreme)
            sc_ = Sc[i, h] if (p['hedge'] == 'spot' and np.isfinite(Sc[i, h])) else p.get('last_s', np.nan)
            ec = pair_eq(p, i, Ac[i, h], Bc[h], sc_)
            mmc = mmr[i] * p['qc'] * Ac[i, h] + (BTC_MMR * p['qb'] * Bc[h] if p['hedge'] == 'btc' else 0.0)
            if G.get('liq_mode') == 'close_only':    # VERIFIER: optimistic bound, no intrabar check at all
                ew, mm = ec, mmc
            if ew <= mm or ec <= mmc:
                if G.get('log_trades') is not None:
                    G['log_trades'].append((p['h_open'], h, G['syms'][i], p['s'], p['m0'], 0.0))
                if G.get('log_liq') is not None:
                    G['log_liq'].append((h, G['syms'][i], s, p['pe'], Padv, Ac[i, h], p.get('se'), Sadv, Sc[i, h], ew, mm, p['m'], p['qc'] * p['pe']))
                pos.pop(i)
                nliq += 1
                continue                         # margin (incl. the liquidation fee) is lost
            p['last'] = Ac[i, h]
            if p['hedge'] == 'spot' and np.isfinite(Sc[i, h]):
                p['last_s'] = Sc[i, h]
            tro += max(min(ew, ec), 0.0)            # a sub-account cannot be worth less than 0 to the account
            clo += max(ec, 0.0)
        eq_close[k] = clo; eq_trough[k] = tro
    # close everything at the end (not counted as a trade exit cost beyond fees)
    for i in list(pos.keys()):
        hl = min(h1 - 1, last_h[i])
        close(i, hl, Ac[i, hl], Bc[hl], pos[i].get('last_s', np.nan))
    eq_close[-1] = cash
    return eq_close, eq_trough, ntr, nliq, fund_sum, cost_sum


def metrics(eqc, eqt, t0, t1):
    nd = (t1 - t0).days
    daily = eqc[23::24][:nd]
    daily = np.r_[1.0, daily]
    dret = daily[1:] / np.maximum(daily[:-1], 1e-300) - 1.0
    dret = np.where(daily[:-1] > 0, dret, 0.0)
    peak = np.maximum.accumulate(np.r_[1.0, eqc])[:-1]
    dd = float(min((np.minimum(eqt, eqc) / peak - 1.0).min(), 0.0))
    final = float(eqc[-1])
    cagr = final ** (365.0 / nd) - 1.0 if final > 0 else -1.0
    sd = dret.std()
    sh = float(dret.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0
    yrs = pd.date_range(t0, periods=nd, freq='D').year
    years = {}
    for y in np.unique(yrs):
        mm = yrs == y
        years[int(y)] = float(np.prod(1 + dret[mm]) - 1)
    return cagr, dd, float(dret.min()), sh, final, years


def run_one(args):
    cfg, L = args
    out = {'thr_in': cfg[0], 'thr_out_f': cfg[1], 'K': cfg[2], 'hedge': cfg[3], 'side': cfg[4], 'universe': cfg[5], 'L': L}
    for tag, (t0, t1) in (('is', (IS0, IS1)), ('oos', (IS1, OOS1))):
        eqc, eqt, ntr, nliq, fs, cs = simulate(cfg, L, t0, t1)
        cagr, dd, wd, sh, fin, yrs = metrics(eqc, eqt, t0, t1)
        out.update({f'cagr_{tag}': cagr, f'maxdd_{tag}': dd, f'worst_day_{tag}': wd, f'sharpe_{tag}': sh,
                    f'final_{tag}': fin, f'trades_{tag}': ntr, f'liq_{tag}': nliq, f'funding_{tag}': fs, f'costs_{tag}': cs,
                    f'years_{tag}': json.dumps(yrs)})
    return out


def init():
    load()


if __name__ == '__main__':
    if sys.argv[1] == 'build':
        build()
        sys.exit()
    if sys.argv[1] == 'build_mark':
        build_mark()
        sys.exit()
    if sys.argv[1] == 'mark':          # re-run the whole grid with MARK-price extremes for liquidation / trough
        G['use_mark'] = True
    grid = []
    for thr_in, tof, K, hedge, side, uni in itertools.product([0.5, 1.0, 2.0, 4.0], [0.0, 0.5], [1, 3, 10],
                                                             ['btc', 'none', 'spot'], ['both', 'pos', 'neg'], ['all', 'okx']):
        if hedge == 'spot' and side != 'pos':
            continue
        grid.append((thr_in, tof, K, hedge, side, uni))
    jobs = [(c, L) for c in grid for L in LEVS]
    print('configs', len(grid), 'runs', len(jobs), flush=True)
    t = time.time()
    rows = []
    load()                                   # loaded once; forked workers share the arrays copy-on-write
    with Pool(int(sys.argv[2]) if len(sys.argv) > 2 else 3) as pool:
        for j, r in enumerate(pool.imap_unordered(run_one, jobs, chunksize=4)):
            rows.append(r)
            if j % 100 == 0:
                print(j, round(time.time() - t), 's', flush=True)
    df = pd.DataFrame(rows)
    tag = 'grid_slow_mark' if G.get('use_mark') else 'grid_slow'
    df.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), tag + '.csv.gz'), index=False)
    df.to_parquet(os.path.join(D, tag + '.parquet'))
    print('saved', len(df), round(time.time() - t), 's')
