"""Adversarial verification of the 'combo_tsmom' direction (diversified TSMOM + book + BTC/ETH carry).

Runs on the verifier's re-run of the researcher's pipeline (rerun/, bit-identical results.json) and adds:
  V1  delisting / not-live exits while a TSMOM position is held (survivorship handling)
  V2  small-account lot feasibility of each SLEEVE inside the mixes (TSMOM sleeve re-run with OKX lot rounding at
      its real sleeve equity a_t * E * L; book sleeve: average position notional vs OKX minimum order notional)
  V3  book fill model: Hermes books assume a fixed 60 % maker share (maker_fill_ratio 0.6), not 'maker only when
      price trades through'. Pessimistic bound: all book fills taker (fees x 5/3.2, full half-spread)
  V4  carry on OKX funding (deployment venue) inside every mix, re-weighted IS
  V5  TSMOM selection vs grid: share of the 72 configs for which each mix passes item 1
  V6  1-hour execution delay (signal at the 00:00 close, fill at the 01:00 price) from Binance 1h klines
  V7  hourly (instead of daily all-at-once) simultaneous intrabar trough for TSMOM OOS -> 1x max drawdown
Outputs: verify_results.json, verify_bar.csv
"""
import json, sys, os
import numpy as np, pandas as pd

VW = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_combo_tsmom'
sys.path.insert(0, f'{VW}/rerun')
import tsmom as M

W = M.W
OOS = (M.OOS_START, M.OOS_END)
C_IS = ('2023-08-03', '2024-12-31')
LEVS = [1, 2, 3, 5, 8, 10, 15, 20]
BOOK = '/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv'
out = {}


def sh(x):
    return M.sharpe(x)


def yret(x, y):
    return float((1 + x[str(y)]).prod() - 1)


P = M.load()
D = P['dates']; S = np.array(P['syms'])
cfg = dict(N=30, lbs=[10, 20, 40], kind='z', hl=60)
BAND = 0.5
cache = {}
Wt, mem, rank = M.target_weights(cfg, cache)
mmr, imr, cv, lot, minq = M.okx_arrays()
T = M.backtest(Wt, rank, band=BAND, mmr=mmr, imr=imr)


def bt_record(Wt, rank, band=0.0, start=M.IS_START, end=M.OOS_END):
    """Copy of M.backtest (L=1, no lots) that also records pre-trade and post-trade weights per day."""
    t0 = D.searchsorted(pd.Timestamp(start, tz='UTC'))
    t1 = D.searchsorted(pd.Timestamp(end, tz='UTC'), side='right')
    r = np.nan_to_num(P['r']); F = P['F']
    slip = M.slip_bp(np.nan_to_num(rank, nan=40), P['syms'])
    Sn = Wt.shape[1]
    w = np.zeros(Sn)
    rows, wb, wa, days = [], [], [], []
    for t in range(t0 - 1, t1 - 1):
        tgt = Wt[t]
        if band > 0:
            na = max((np.abs(tgt) > 0).sum(), 1)
            thr = band * np.abs(tgt).sum() / na
            trade = (np.abs(tgt - w) > thr) | ((tgt == 0) & (w != 0))
            new = np.where(trade, tgt, w)
        else:
            new = tgt
        dw = np.abs(new - w)
        cost = np.sum(dw * (M.FEE_TAKER + slip[t]))
        wb.append(w.copy()); wa.append(new.copy()); days.append(t)
        w = new
        pnl = np.sum(w * (r[t + 1] - F[t + 1]))
        ret = pnl - cost
        rows.append((D[t + 1], ret, cost))
        w = w * (1 + r[t + 1]) / (1 + ret)
    df = pd.DataFrame(rows, columns=['d', 'ret', 'cost']).set_index('d')
    return df, np.array(wb), np.array(wa), np.array(days)


Tr, WB, WA, DAYS = bt_record(Wt, rank, band=BAND)
assert np.allclose(Tr.ret.values, T.ret.values), 'recorded backtest differs from researcher backtest'

# ------------------------------------------------------------------ V1 delisting / not-live exits
live = P['live']; Cf = P['Cf']; C = P['C']
ev = []
for k, t in enumerate(DAYS):
    held = np.where(WA[k] != 0)[0]
    for j in held:
        if not live[t + 1, j]:
            # coin not live next day: its return is set to 0 by the engine (position closed at last close)
            nxt = np.where(live[t + 1:, j])[0]
            relive = str(D[t + 1 + nxt[0]].date()) if len(nxt) else None
            ev.append(dict(day=str(D[t + 1].date()), sym=S[j], w=float(WA[k][j]), last_close=float(Cf[t, j]),
                           relive=relive,
                           next_close=float(C[t + 1 + nxt[0], j]) if len(nxt) else None))
out['V1_notlive_exits'] = ev
print('V1 held positions whose coin was not live the next day:', len(ev))
for e in ev:
    print('  ', e)

# ------------------------------------------------------------------ book stream + carry (as in evaluate.py)
B = pd.read_csv(BOOK, index_col=0, parse_dates=True)
B.index = B.index.tz_convert('UTC') if B.index.tz is not None else B.index.tz_localize('UTC')
Cy = pd.read_csv(f'{W}/carry_daily.csv', index_col=0, parse_dates=True)
Cy.index = Cy.index.tz_convert('UTC') if Cy.index.tz is not None else Cy.index.tz_localize('UTC')
idx = B.loc['2023-08-03':'2026-08-31'].index
b = B.loc[idx]
book = pd.DataFrame({'ret': b['return'], 'gross': b.gross, 'fees': b.fees, 'spread': b.spread, 'impact': b.impact,
                     'n': b.n_positions})
carry = Cy.reindex(idx)
ts = T.reindex(idx)
fx = pd.read_csv(f'{W}/okx_funding_btceth.csv', index_col=0, parse_dates=True)
fx.index = fx.index.tz_convert('UTC') if fx.index.tz is not None else fx.index.tz_localize('UTC')
adj = (0.5 * ((fx.okx_BTC - fx.bin_BTC) + (fx.okx_ETH - fx.bin_ETH))).reindex(idx).fillna(0.0)
carry_okx = carry.ret + adj


def weights(rb, rt, rc, scheme):
    v = np.array([rb.loc[C_IS[0]:C_IS[1]].std(), rt.loc[C_IS[0]:C_IS[1]].std(), rc.loc[C_IS[0]:C_IS[1]].std()])
    a = {'inv_vol_3': 1 / v, 'inv_var_3': 1 / v ** 2, 'inv_vol_book_tsmom': np.array([1 / v[0], 1 / v[1], 0.0]),
         'equal_capital_3': np.ones(3), 'book_alone': np.array([1.0, 0, 0])}[scheme]
    return a / a.sum()


def mix(a, rb, rt, rc, L=1, bor=None):
    ell = L * a[2]
    brw = np.maximum(ell - 1, 0) * (bor if bor is not None else carry.borrow_daily)
    return L * (a[0] * rb + a[1] * rt) + ell * rc - brw


def item1(r):
    o = r.loc[OOS[0]:OOS[1]]
    return dict(sharpe_oos=round(sh(o), 3), y2025=round(yret(r, 2025), 4), y2026=round(yret(r, 2026), 4),
                pass_=bool(sh(o) >= 1.5 and yret(r, 2025) > 0 and yret(r, 2026) > 0))


SCHEMES = ['inv_vol_3', 'inv_var_3', 'inv_vol_book_tsmom', 'equal_capital_3', 'book_alone']

# ------------------------------------------------------------------ V3 book all-taker bound, V4 OKX funding
blend = 0.6 * 0.0002 + 0.4 * 0.0005
book_taker = book.ret - book.fees * (0.0005 / blend - 1) - book.spread * (1 / 0.4 - 1)
v34 = {}
for sc in SCHEMES:
    a = weights(book.ret, ts.ret, carry.ret, sc)
    base = item1(mix(a, book.ret, ts.ret, carry.ret))
    a_okx = weights(book.ret, ts.ret, carry_okx, sc)
    okx = item1(mix(a_okx, book.ret, ts.ret, carry_okx))
    a_t = weights(book_taker, ts.ret, carry_okx, sc)
    taker = item1(mix(a_t, book_taker, ts.ret, carry_okx))
    v34[sc] = dict(weights=a.round(4).tolist(), base=base, okx_funding=okx, okx_funding_book_all_taker=taker)
    print('V3/V4', sc, v34[sc])
v34['book_extra_cost_all_taker_ann_oos'] = float((book.ret - book_taker).loc[OOS[0]:OOS[1]].mean() * 365)
v34['book_extra_cost_all_taker_ann_is'] = float((book.ret - book_taker).loc[C_IS[0]:C_IS[1]].mean() * 365)
out['V3_V4_item1'] = v34
print('book all-taker extra cost / yr OOS', v34['book_extra_cost_all_taker_ann_oos'])

# ------------------------------------------------------------------ V2 small-account sleeve feasibility
sup = {'inv_vol_3': 8, 'inv_var_3': 8, 'inv_vol_book_tsmom': 3, 'equal_capital_3': 3}
v2 = {}
t_oos = D.searchsorted(pd.Timestamp(OOS[0], tz='UTC'))
min_notional = cv * minq          # USDT value of the minimum order (today's specs on historical prices)
for sc in ['inv_vol_3', 'inv_vol_book_tsmom', 'equal_capital_3']:
    a = weights(book.ret, ts.ret, carry.ret, sc)
    for E in [1000, 3000, 10000]:
        for L in sorted({1, sup[sc]}):
            Es = a[1] * E                      # TSMOM sleeve equity; positions = L * Wt * Es
            d = M.backtest(Wt, rank, band=BAND, L=L, E0=Es, lots=(cv, minq))
            o = d.loc[OOS[0]:OOS[1]]
            tgt_g = float(T.gross.loc[OOS[0]:OOS[1]].mean() * L)
            # book sleeve: average position notional (USDT) = L * a_b * E * gross / n_positions
            bo = book.loc[OOS[0]:OOS[1]]
            pos_usd = (L * a[0] * E * bo.gross / bo.n.replace(0, np.nan)).dropna()
            # OKX min notional of the TSMOM universe coins on each OOS day
            mn = min_notional[t_oos:][mem[t_oos:]]
            v2[f'{sc}_E{E}_L{L}'] = dict(
                tsmom_sleeve_equity=round(Es, 1), tsmom_sharpe_oos_lots=round(sh(o.ret), 3),
                tsmom_sharpe_oos_nolots=round(sh(T.ret.loc[OOS[0]:OOS[1]]), 3),
                tsmom_gross_achieved_over_target=round(float(o.gross.mean()) / tgt_g, 3),
                tsmom_y2026_lots=round(yret(d.ret, 2026), 4),
                book_avg_position_usd_median=round(float(pos_usd.median()), 2),
                okx_min_order_usd_universe_median=round(float(np.median(mn)), 2),
                okx_min_order_usd_universe_p90=round(float(np.quantile(mn, 0.9)), 2))
            print('V2', sc, E, L, v2[f'{sc}_E{E}_L{L}'])
out['V2_small_account'] = v2

# ------------------------------------------------------------------ V5 grid of TSMOM configs inside each mix
GC = pd.read_csv(f'{W}/grid_combo.csv')
v5 = {}
for sc, g in GC.groupby('scheme'):
    p = (g.sharpe_oos >= 1.5) & (g.y2025 > 0) & (g.y2026 > 0)
    v5[sc] = dict(n=len(g), frac_item1=round(float(p.mean()), 3), sharpe_oos_median=round(float(g.sharpe_oos.median()), 3),
                  frac_2026_pos=round(float((g.y2026 > 0).mean()), 3),
                  selected=g[g.tsmom == 'N30_short_z_hl60_b0.5'][['sharpe_oos', 'y2025', 'y2026']].round(4).to_dict('records'))
    # item 1 with OKX funding for the carry, over the grid
    Gd = pd.read_csv(f'{W}/grid_tsmom_daily.csv', index_col=0, parse_dates=True)
    Gd.index = Gd.index.tz_convert('UTC') if Gd.index.tz is not None else Gd.index.tz_localize('UTC')
    ok = []
    for name in Gd.columns:
        rt = Gd[name].reindex(idx)
        a = weights(book.ret, rt, carry_okx, sc)
        ok.append(item1(mix(a, book.ret, rt, carry_okx))['pass_'])
    v5[sc]['frac_item1_okx_funding'] = round(float(np.mean(ok)), 3)
    print('V5', sc, v5[sc])
out['V5_grid'] = v5

# ------------------------------------------------------------------ V6/V7 hourly checks
H1 = f'{VW}/h1/h1_oos.parquet'
if os.path.exists(H1):
    h = pd.read_parquet(H1)
    h['ts'] = pd.to_datetime(h.t, unit='ms', utc=True)
    h = h.drop_duplicates(['sym', 't'])
    h['d'] = h.ts.dt.normalize(); h['hr'] = h.ts.dt.hour
    first = h[h.hr == 0].set_index(['d', 'sym'])
    x1 = (first.c / first.o - 1).unstack().reindex(index=D, columns=S)          # first-hour return of day d
    cov_mask = x1.notna().values
    x1v = x1.fillna(0.0).values
    r = np.nan_to_num(P['r'])
    ret6 = Tr.ret.copy()
    covered, total = 0.0, 0.0
    for k, t in enumerate(DAYS):
        d1 = t + 1
        if D[d1] < pd.Timestamp(OOS[0], tz='UTC'):
            continue
        wb, wa = WB[k], WA[k]
        chg = np.abs(wa - wb)
        total += chg.sum(); covered += chg[cov_mask[d1]].sum()
        # exact per-coin P&L of the day with the trade at the 01:00 price instead of 00:00
        x = x1v[d1]
        base = np.sum(wa * r[d1])
        tr = wa != wb
        dl = np.sum(np.where(tr, wb * x + wa * ((1 + r[d1]) / (1 + x) - 1), wa * r[d1]))
        ret6.iloc[k] += dl - base
    o6 = ret6.loc[OOS[0]:OOS[1]]
    v6 = dict(tsmom_sharpe_oos_base=round(sh(Tr.ret.loc[OOS[0]:OOS[1]]), 3), tsmom_sharpe_oos_fill_0100=round(sh(o6), 3),
              y2025=round(yret(ret6, 2025), 4), y2026=round(yret(ret6, 2026), 4),
              traded_notional_with_hourly_data=round(covered / total, 4))
    ts6 = ret6.reindex(idx)
    for sc in SCHEMES[:-1]:
        a = weights(book.ret, ts6, carry.ret, sc)
        v6[sc] = item1(mix(a, book.ret, ts6, carry.ret))
    out['V6_fill_delay_1h'] = v6
    print('V6', v6)

    # V7 hourly simultaneous trough (Binance 1h), per day in OOS: min over hours of sum_j w_j * adverse(hour)
    hl = h.set_index(['ts', 'sym'])
    lo = (hl.l.unstack()); hi = (hl.h.unstack())
    ib7 = T.ib.copy()
    n_fb = 0
    for k, t in enumerate(DAYS):
        d1 = t + 1
        if D[d1] < pd.Timestamp(OOS[0], tz='UTC'):
            continue
        wa = WA[k]
        js = np.where(wa != 0)[0]
        if not len(js):
            continue
        day = D[d1]
        hrs = pd.date_range(day, periods=24, freq='h')
        path = np.zeros(24); fb = 0.0
        for j in js:
            s = S[j]; c0 = Cf[t, j]
            if s in lo.columns and np.isfinite(c0):
                l_ = lo[s].reindex(hrs).values; h_ = hi[s].reindex(hrs).values
                if np.isfinite(l_).all():
                    adv = np.minimum(wa[j] * (l_ / c0 - 1), wa[j] * (h_ / c0 - 1))
                    # adverse within the hour; the path carries the running close-to-date move via extremes only
                    path += adv
                    continue
            fb += min(wa[j] * P['lo'][d1, j], wa[j] * P['hi'][d1, j]); n_fb += 1
        cost = T.cost.iloc[k]
        ib7.iloc[k] = min(path.min() + fb - cost, T.ret.iloc[k], -cost)
    o = T.loc[OOS[0]:OOS[1]]
    res7 = {}
    for name, ib in [('daily_all_at_once', T.ib), ('hourly_simultaneous', ib7)]:
        E, pk, mdd = 1.0, 1.0, 0.0
        for rr, ii in zip(o.ret.values, ib.loc[OOS[0]:OOS[1]].values):
            mdd = max(mdd, 1 - E * (1 + ii) / pk); E *= 1 + rr; pk = max(pk, E); mdd = max(mdd, 1 - E / pk)
        res7[f'maxdd_1x_{name}'] = round(mdd, 4)
    res7['ib_20251010_daily'] = round(float(T.ib.loc['2025-10-10']), 4)
    res7['ib_20251010_hourly'] = round(float(ib7.loc['2025-10-10']), 4)
    res7['position_days_fallback_to_daily'] = n_fb
    out['V7_hourly_trough'] = res7
    print('V7', res7)

    # V8 leverage with the hourly TSMOM trough; book intrabar shock on 2025-10-10 that breaks each mix's leverage
    ib7.to_frame('ib_hourly').to_csv(f'{VW}/tsmom_ib_hourly.csv')
    BOOK_MMR, BOOK_IMR = 0.015, 1 / 20.0
    CMM, CBM, CIM, CBI = 0.004 + 0.02, 0.02, 0.01 + 0.02, 0.10
    sig_b = book.ret.ewm(halflife=20, min_periods=10).std().shift(1).bfill()
    b_ib = np.minimum(book.ret, 0) - sig_b

    def lev_sim(r, ib, mm, im):
        E, peak, maxdd, liq = 1.0, 1.0, 0.0, None
        for d, ri, ii, mi in zip(r.index, r.values, ib.values, mm.values):
            trough = E * (1 + ii)
            if trough <= E * mi or trough <= 0:
                return dict(maxdd=1.0, liq=str(d.date()), openable=bool(im.max() <= 1))
            maxdd = max(maxdd, 1 - trough / peak); E *= 1 + ri; peak = max(peak, E); maxdd = max(maxdd, 1 - E / peak)
        return dict(maxdd=round(float(maxdd), 4), liq=liq, openable=bool(im.max() <= 1))

    O_ = slice(OOS[0], OOS[1])
    tsx = pd.DataFrame({'ret': T.ret, 'ib': ib7, 'mm': T.mm, 'im': T.im}).reindex(idx)
    def mix_lev(a, L, bshock=0.0, tsib=None):
        tib = (tsib if tsib is not None else tsx.ib)[O_]
        bib = b_ib.copy(); bib.loc['2025-10-10'] -= bshock; bib = bib[O_]
        ell = L * a[2]; brw = np.maximum(ell - 1, 0) * carry.borrow_daily[O_]
        r = L * (a[0] * book.ret[O_] + a[1] * tsx.ret[O_]) + ell * carry.ret[O_] - brw
        ib = L * (a[0] * bib + a[1] * tib) + ell * carry.ib[O_] - brw
        mm = L * (a[0] * BOOK_MMR * book.gross[O_] + a[1] * tsx.mm[O_]) + CMM * ell + CBM * max(ell - 1, 0)
        im = L * (a[0] * BOOK_IMR * book.gross[O_] + a[1] * tsx.im[O_]) + CIM * ell + CBI * max(ell - 1, 0)
        return lev_sim(r, ib, mm, im)
    To = T.loc[O_]
    v8 = {'tsmom_alone_hourly_trough': {L: lev_sim(L * To.ret, L * ib7.loc[O_], L * To.mm, L * To.im) for L in LEVS[:4]},
          'tsmom_half_kelly_is': 1.642}
    for sc, hk in [('inv_vol_book_tsmom', 8.0), ('equal_capital_3', 10.75), ('inv_vol_3', 152.5)]:
        a = weights(book.ret, ts.ret, carry.ret, sc)
        v8[sc] = {'hourly_tsmom_trough': {L: mix_lev(a, L) for L in LEVS}}
        # smallest extra book intrabar loss (unit) on 2025-10-10 that pushes the researcher's supportable L over 35 %
        for L in ([3] if sc != 'inv_vol_3' else [8]):
            br = None
            for x in np.arange(0.0, 0.401, 0.005):
                z = mix_lev(a, L, bshock=x, tsib=T.ib.reindex(idx))
                if z['maxdd'] > 0.35 or z['liq']:
                    br = round(float(x), 3); break
            v8[sc][f'book_1010_shock_breaking_L{L}_daily_proxy'] = br
    out['V8_leverage'] = v8
    print('V8', json.dumps(v8, default=str))

json.dump(out, open(f'{VW}/verify_results.json', 'w'), indent=1, default=str)
print('saved')
