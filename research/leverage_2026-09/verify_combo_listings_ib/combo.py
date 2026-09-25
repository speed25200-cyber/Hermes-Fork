"""One OKX cross-margin account holding both sleeves, simulated hourly.
Listing sleeve: the researcher/verifier hourly simulator logic (sim_v.simulate, config short d0=72h d1=7d, BTC hedge
beta 1, stop 50%, newtok, K=5), sized on the COMBINED account equity with sim leverage L*w_l (w_l = 0.397; sim L=1 is the
'short-leg gross cap 1' sleeve the main loop combined). Prices: 'hybrid2' (OKX REST + OKX trade-archive rebuild) or 'hybrid'.
Book sleeve: at each UTC day start, book capital X_d = L*w_b*E (w_b = 0.603); day-d P&L X_d * r_book(d) is credited at the
23:00 bar close. Its gross notional X_d * gross(d) carries OKX maintenance margin mmr_book. Its intraday path is unknown and
is replaced, at every hour h of day d, by an intrabar trough ib[d, h] (fraction of book capital, <= 0) chosen by 'book_model'
(see book_ib()); the trough is added to the listing sleeve's intrabar worst of the SAME hour (legs at adverse extremes:
coin shorts at hourly highs, BTC long at hourly low), and compared with total maintenance margin (tier MMR) -> liquidation
(equity to 0, stop). Intrabar max drawdown = max over hours of 1 - worst_equity / running peak of hourly close equity.
Initial-margin usage (openability) is tracked with OKX max-leverage IMRs."""
import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_listings')
import numpy as np, pandas as pd
import sim_v
from sim_v import Data, gh, G0, HMS, FEE, SLIP_COIN, SLIP_BTC, OOS_START, OOS_END

W_B, W_L = 0.603, 0.397
CFG = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
BOOKCSV = '/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv'
_B = pd.read_csv(BOOKCSV, index_col=0, parse_dates=True)
_B.index = _B.index.tz_localize(None)
BOOK = _B[['return', 'gross', 'net', 'n_positions']].copy()
BOOK['sig'] = BOOK['return'].ewm(halflife=20, min_periods=10).std().shift(1).bfill()
_P = None


def book_ib(model, days):
    """(len(days), 24) intrabar trough of the book as a fraction of book capital, <= 0.
    close : 0 (book only at daily closes: the main loop's assumption)
    S1/S3 : min(r,0) - k*sigma_ewm(prior days)            (statistical, k = 1 or 3)
    C     : min(r,0) + gross*C[d,h] + net term               (p99 random beta-neutral book of actual breadth, synchronous extremes)
    B     : min(r,0) + gross*B[d,h] + net term               (adversarial cross-sectional sort, synchronous extremes)
    A     : min(r,0) + gross*A[d,h]                          (adversarial, long legs at hourly lows AND short legs at hourly highs)
    net term = min(0, net*mkt_low[d,h], net*mkt_high[d,h]) (equal-weight universe basket)."""
    global _P
    b = BOOK.reindex(days).fillna(0.0)
    out = np.zeros((len(days), 24))
    if model == 'close':
        return out
    r = b['return'].values
    base = np.minimum(r, 0.0)[:, None]
    if model == 'LEG':                          # each leg's realized daily loss immediate, the other leg's gain only at the close
        Bf = _B.reindex(days).fillna(0.0)
        return np.repeat((np.minimum(Bf.pnl_long.values, 0) + np.minimum(Bf.pnl_short.values, 0))[:, None], 24, 1)
    if model in ('S1', 'S3'):
        k = 1.0 if model == 'S1' else 3.0
        return np.repeat(base - k * b['sig'].values[:, None], 24, 1)
    if _P is None:
        _P = pd.read_parquet('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_combo_listings_ib/book_paths.parquet')
    P = _P.set_index(['day', 'hour'])
    col = model[0]
    X = P[col].unstack('hour').reindex(days).values
    lo = P['mlo'].unstack('hour').reindex(days).values
    hi = P['mhi'].unstack('hour').reindex(days).values
    g, n = b['gross'].values[:, None], b['net'].values[:, None]
    if np.isnan(X).any():
        raise ValueError(f'book path {model} missing on {int(np.isnan(X).any(1).sum())} days')
    ib = base + g * X
    if col != 'A':
        ib = ib + np.minimum(0.0, np.minimum(n * lo, n * hi))
    ib = np.minimum(ib, 0.0)
    if model.endswith('x10'):                 # extra scenario flag handled by caller
        pass
    return ib


def combo_sim(Dt, L, book_model='close', start=OOS_START, end=OOS_END, wb=W_B, wl=W_L, cfg=CFG, mmr_coin=0.02, mmr_btc=0.004,
              mmr_book=0.02, imr_coin=0.05, imr_btc=0.01, imr_book=0.05, cost_mult=1.0, lat=0, stop_slip=0.0,
              stop_worst=False, extra_book_shock=None, record=False, exclude=None, excl_months=None):
    d0, d1, side, beta, stop, K = cfg['d0'], cfg['d1'], cfg['side'], cfg['beta'], cfg['stop'], cfg['K']
    Ll = L * wl
    gs, ge = gh(start), gh(end)
    fee_c = (FEE + SLIP_COIN) * cost_mult
    fee_b = (FEE + SLIP_BTC) * cost_mult
    ent = Dt.g0 + d0 + lat
    elig = (ent >= gs) & (ent < ge) & (d0 + lat < Dt.H)
    if cfg['uni'] == 'newtok':
        elig &= Dt.newtok
    entries = {}
    for i in np.where(elig)[0]:
        k = d0 + lat
        if Dt.okx_on[i, k] and np.isfinite(Dt.o[i, k]) and np.isfinite(Dt.bo[i, k]):
            if exclude is not None and Dt.ev.sym.values[i] in exclude:
                continue
            if excl_months is not None and str(pd.Timestamp(G0 + int(ent[i]) * HMS, unit='ms').to_period('M')) in excl_months:
                continue
            entries.setdefault(int(ent[i]), []).append(i)
    days = pd.date_range(start, end, freq='D', inclusive='left')
    bk = BOOK.reindex(days).fillna(0.0)
    br, bg = bk['return'].values, bk['gross'].values
    ib = book_ib(book_model, days)
    if extra_book_shock:
        for dstr, x in extra_book_shock.items():
            j = days.get_loc(pd.Timestamp(dstr))
            ib[j, :] = np.minimum(ib[j, :], -x) if x > 0 else ib[j, :]
    t_start = gh(start)
    E, peak, maxdd, liq, liq_t = 1.0, 1.0, 0.0, False, None
    pos, trades = [], []
    n = ge - gs
    eq_close = np.full(n, np.nan); eq_worst = np.full(n, np.nan); im_use = np.zeros(n); mm_use = np.zeros(n)
    comp_l = np.zeros(n); comp_b = np.zeros(n)
    X = 0.0
    for t in range(gs, ge):
        di, hh = (t - t_start) // 24, (t - t_start) % 24
        if hh == 0:
            X = L * wb * E                         # book capital for the day
        # 1) scheduled / pending exits at the open
        keep = []
        for p in pos:
            k = t - Dt.g0[p['i']]
            px = Dt.o[p['i'], k] if k < Dt.H else np.nan
            if t >= p['xt'] or p['pending'] or not np.isfinite(px):
                if not np.isfinite(px):
                    px = p['last']
                E += p['q'] * (px - p['last']) - abs(p['q']) * px * fee_c
                bpx = Dt.bo[p['i'], k] if k < Dt.H and np.isfinite(Dt.bo[p['i'], k]) else p['blast']
                E += p['bq'] * (bpx - p['blast']) - abs(p['bq']) * bpx * fee_b
                p['tr'].update(exit_px=px, exit_t=t, ret=side * (px / p['entry'] - 1))
                trades.append(p['tr'])
            else:
                keep.append(p)
        pos = keep
        # 2) entries, sized on combined equity with sleeve leverage Ll
        for i in entries.get(t, []):
            k = t - Dt.g0[i]
            px, bpx = Dt.o[i, k], Dt.bo[i, k]
            if (Dt.vol_n[i, k - 2] >= 12) if k >= 2 else False:
                scale = np.clip(Dt.sigma_ref / max(Dt.vol_d[i, k - 2], 1e-9), 0.25, 1.0)
            else:
                scale = 0.5
            gross = sum(abs(p['q']) * p['last'] for p in pos)
            notional = min(E * Ll * scale / K, max(E * Ll - gross, 0.0))
            if notional <= 0:
                continue
            q = side * notional / px
            bq = -side * beta * notional / bpx
            E -= abs(q) * px * fee_c + abs(bq) * bpx * fee_b
            sp = px * (1 + stop) if (stop is not None and side < 0) else (px * (1 - stop) if stop is not None else None)
            tr = dict(sym=Dt.ev.sym.values[i], i=int(i), entry_t=t, entry_px=px, notional=notional, E=E)
            pos.append(dict(i=i, q=q, last=px, entry=px, bq=bq, blast=bpx, xt=Dt.g0[i] + d1 + lat, stop_px=sp, pending=False, tr=tr))
        # 3) intrabar worst of the listing legs + book trough of the same hour; maintenance margin
        wl_pnl, mm, im = 0.0, 0.0, 0.0
        stop_fills = []
        for p in pos:
            k = t - Dt.g0[p['i']]
            o_, h_, l_ = Dt.o[p['i'], k], Dt.h[p['i'], k], Dt.l[p['i'], k]
            if not np.isfinite(h_):
                continue
            adv = h_ if p['q'] < 0 else l_
            if p['stop_px'] is not None and not p['pending']:
                hit = (h_ >= p['stop_px']) if p['q'] < 0 else (l_ <= p['stop_px'])
                if hit:
                    if lat == 0:
                        fill = max(o_, p['stop_px']) if p['q'] < 0 else min(o_, p['stop_px'])
                        if stop_slip > 0:
                            fill = min(h_, fill * (1 + stop_slip)) if p['q'] < 0 else max(l_, fill * (1 - stop_slip))
                        if stop_worst:
                            fill = h_ if p['q'] < 0 else l_
                        adv = fill
                        stop_fills.append((p, fill))
                    else:
                        p['pending'] = True
            wl_pnl += p['q'] * (adv - p['last'])
            mm += abs(p['q']) * adv * mmr_coin
            im += abs(p['q']) * adv * imr_coin
            bo_, bh_, bl_ = Dt.bo[p['i'], k], Dt.bh[p['i'], k], Dt.bl[p['i'], k]
            if np.isfinite(bh_) and p['bq'] != 0:
                badv = bl_ if p['bq'] > 0 else bh_
                wl_pnl += p['bq'] * (badv - p['blast'])
                mm += abs(p['bq']) * badv * mmr_btc
                im += abs(p['bq']) * badv * imr_btc
        b_pnl = X * ib[di, hh]
        mm += X * bg[di] * mmr_book
        im += X * bg[di] * imr_book
        worst = E + wl_pnl + b_pnl
        comp_l[t - gs], comp_b[t - gs] = wl_pnl, b_pnl
        maxdd = max(maxdd, 1 - worst / peak)
        eq_worst[t - gs] = worst
        mm_use[t - gs] = mm / max(worst, 1e-12)
        im_use[t - gs] = im / max(E, 1e-12)
        if worst <= mm and (pos or X * bg[di] > 0):
            liq, liq_t = True, t
            eq_close[t - gs:] = 0.0; eq_worst[t - gs:] = 0.0; maxdd = 1.0
            break
        for p, fill in stop_fills:
            k = t - Dt.g0[p['i']]
            E += p['q'] * (fill - p['last']) - abs(p['q']) * fill * fee_c
            bpx = Dt.bc[p['i'], k] if np.isfinite(Dt.bc[p['i'], k]) else p['blast']
            E += p['bq'] * (bpx - p['blast']) - abs(p['bq']) * bpx * fee_b
            p['tr'].update(exit_px=fill, exit_t=t, ret=side * (fill / p['entry'] - 1), reason='stop')
            trades.append(p['tr'])
        if stop_fills:
            sf = {id(p) for p, _ in stop_fills}
            pos = [p for p in pos if id(p) not in sf]
        # 4) marks, funding; book P&L at the day's last close
        for p in pos:
            k = t - Dt.g0[p['i']]
            c_ = Dt.c[p['i'], k]
            if np.isfinite(c_):
                E += p['q'] * (c_ - p['last']); p['last'] = c_
                E -= p['q'] * c_ * Dt.fund[p['i'], k]
            bc_ = Dt.bc[p['i'], k]
            if np.isfinite(bc_) and p['bq'] != 0:
                E += p['bq'] * (bc_ - p['blast']); p['blast'] = bc_
        if hh == 23:
            E += X * br[di]
        eq_close[t - gs] = E
        peak = max(peak, E)
        if E <= 0:
            liq, liq_t = True, t
            eq_close[t - gs:] = 0.0; maxdd = 1.0
            break
    idx = pd.date_range(start, periods=n, freq='h')
    eq = pd.Series(eq_close, idx).ffill().fillna(1.0)
    daily = eq.resample('D').last()
    ret = daily.pct_change().fillna(daily.iloc[0] - 1.0)
    ew = pd.Series(eq_worst, idx)
    out = dict(ret=ret, maxdd=float(maxdd), liq=liq, liq_time=str(pd.Timestamp(G0 + liq_t * HMS, unit='ms')) if liq else None,
               trades=trades, final=float(eq.iloc[-1]))
    dd = 1 - ew / eq.cummax()
    out['maxdd_time'] = str(dd.idxmax()) if dd.notna().any() else None
    if dd.notna().any():
        j = int(np.nanargmax(dd.values))
        out['maxdd_parts'] = dict(listing_intrabar=float(comp_l[j] / eq.cummax().iloc[j]), book_trough=float(comp_b[j] / eq.cummax().iloc[j]))
    out['im_use_max'] = float(np.nanmax(im_use)); out['mm_over_worst_max'] = float(np.nanmax(mm_use[np.isfinite(mm_use)]))
    if record:
        out.update(eq=eq, eq_worst=ew, im_use=pd.Series(im_use, idx), comp_l=pd.Series(comp_l, idx), comp_b=pd.Series(comp_b, idx))
    return out


def sharpe(r):
    r = np.asarray(r, float); s = r.std(ddof=1)
    return float(r.mean() / s * np.sqrt(365)) if s > 0 else 0.0


def summ(res):
    r = res['ret']
    y = r.groupby(r.index.year).apply(lambda x: float(np.prod(1 + x) - 1))
    e = (1 + r).cumprod()
    g = float(np.prod(1 + r))
    return dict(sharpe=round(sharpe(r), 3), cagr=round(g ** (365 / len(r)) - 1, 4) if g > 0 else -1.0,
                daily_maxdd=round(float((1 - e / e.cummax()).max()), 4), ib_maxdd=round(res['maxdd'], 4), liq=res['liq'],
                liq_time=res['liq_time'], maxdd_time=res.get('maxdd_time'), parts={k: round(v, 4) for k, v in res.get('maxdd_parts', {}).items()},
                im_use_max=round(res['im_use_max'], 3), years={int(k): round(v, 4) for k, v in y.items()})
