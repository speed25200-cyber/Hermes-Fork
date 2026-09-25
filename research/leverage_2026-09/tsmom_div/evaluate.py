"""Main evaluation: pre-registered success bar for (a) the IS-selected diversified TSMOM portfolio and (b) its
combination with the existing market-neutral book and fully funded BTC+ETH carry, with weights chosen on IS only.

Inputs: grid_tsmom.csv / tsmom_selected.json (grid.py), carry_daily.csv (carry_stream.py),
        book OOS daily returns (Hermes report, read-only).
Outputs: results.json, results_summary.csv, leverage_table.csv, combo_daily.csv
"""
import json
import numpy as np, pandas as pd
import tsmom as M

W = M.W
BOOK = '/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv'
LEVS = [1, 2, 3, 5, 8, 10, 15, 20]
LBS = {'all': [10, 20, 40, 60, 120], 'short': [10, 20, 40], 'long': [40, 60, 120]}
OOS = (M.OOS_START, M.OOS_END)
C_IS = ('2023-08-03', '2024-12-31')        # combination weighting window (book stream is live from 2023-08-03)
BOOK_MMR = 0.015                           # mean OKX tier-1 MMR of the liquid-perp universe (see tsmom_selected run)
BOOK_IMR = 1 / 20.0
CARRY_MM_PER_L = 0.004 + 0.02              # perp tier-1 MMR (BTC/ETH) + 2 % collateral haircut on the spot leg
CARRY_BORROW_MMR = 0.02
CARRY_IM_PER_L = 0.01 + 0.02              # perp IMR (BTC/ETH tier 1) + spot collateral haircut (adjusted equity)
CARRY_BORROW_IMR = 0.10


def sh(x):
    return M.sharpe(x)


def yret(x, y):
    x = x[str(y)]
    return float((1 + x).prod() - 1)


def lomo_min(x):
    x = x.loc[OOS[0]:OOS[1]]
    m = x.index.tz_localize(None).to_period('M')
    vals = {str(p): sh(x[m != p]) for p in m.unique()}
    k = min(vals, key=vals.get)
    return vals[k], k


def lev_sim(r, ib, mm, im):
    """Daily path at a given leverage (series already levered). Liquidation when the intrabar trough equity falls
    to the maintenance requirement (cross margin, tier-1 MMR); drawdown measured on intrabar troughs."""
    E, peak, maxdd = 1.0, 1.0, 0.0
    liq = None
    for d, ri, ii, mi in zip(r.index, r.values, ib.values, mm.values):
        trough = E * (1 + ii)
        if trough <= E * mi or trough <= 0:
            liq = str(d.date())
            maxdd = 1.0
            E = 0.0
            break
        maxdd = max(maxdd, 1 - trough / peak)
        E *= 1 + ri
        peak = max(peak, E)
        maxdd = max(maxdd, 1 - E / peak)
    yrs = len(r) / 365.25
    cagr = E ** (1 / yrs) - 1 if E > 0 else -1.0
    return dict(maxdd_intrabar=float(maxdd), liquidated=liq, final=float(E), cagr=float(cagr),
                max_imr_use=float(im.max()), openable=bool(im.max() <= 1.0))


def kelly(fn_r, r_unit):
    """Half-Kelly: (i) classic mu/sigma^2 of unit returns; (ii) growth-optimal L (max mean log growth, including any
    non-linear cost such as borrowing) found on a fine grid; half of each; the bar uses the smaller."""
    mu, var = r_unit.mean(), r_unit.var()
    classic = mu / var if var > 0 else 0.0
    grid = np.concatenate([np.arange(0.05, 5, 0.05), np.arange(5, 60, 0.5), np.arange(60, 400, 5)])
    g = []
    for L in grid:
        rr = fn_r(L)
        g.append(np.log1p(rr).mean() if (rr > -1).all() else -np.inf)
    Lopt = float(grid[int(np.argmax(g))]) if max(g) > 0 else 0.0
    return dict(kelly_classic=float(classic), kelly_growth_opt=Lopt,
                half_kelly=float(min(classic, Lopt) / 2))


def supportable(lev_rows, hk):
    ok = [r['L'] for r in lev_rows if r['maxdd_intrabar'] <= 0.35 and r['liquidated'] is None and r['openable']
          and r['L'] <= hk]
    return max(ok) if ok else 0


# ------------------------------------------------------------------ (a) TSMOM
sel = json.load(open(f'{W}/tsmom_selected.json'))
cfg = dict(N=int(sel['N']), lbs=LBS[sel['lbs']], kind=sel['kind'], hl=int(sel['hl']))
band = float(sel['band'])
print('selected', sel['name'])
cache = {}
Wt, mem, rank = M.target_weights(cfg, cache)
mmr, imr, cv, lot, minq = M.okx_arrays()
T = M.backtest(Wt, rank, band=band, mmr=mmr, imr=imr)
Tr = T.ret
res = {'tsmom': {'config': sel['name'], 'is': M.stats(T, M.IS_START, M.IS_END), 'oos': M.stats(T, *OOS),
                 'years': {y: yret(Tr, y) for y in range(2022, 2027)},
                 'trades_oos': int(T.loc[OOS[0]:OOS[1], 'ntrades'].sum()),
                 'mean_mmr_frac_unit': float(T.mm.mean()), 'max_imr_unit': float(T.im.max())}}
print(json.dumps(res['tsmom'], indent=1, default=str))

# robustness
lm, lm_month = lomo_min(Tr)
P = M.load()
S = np.array(P['syms'])
t0 = P['dates'].searchsorted(pd.Timestamp(OOS[0], tz='UTC'))
oos_coins = list(S[mem[t0:].any(0)])
loco = {}
loco_streams = {}
for c in oos_coins:
    c2 = dict(cfg, exclude=c)
    W2, _, rk2 = M.target_weights(c2, cache)
    d2 = M.backtest(W2, rk2, band=band, mmr=mmr, imr=imr)
    loco[c] = sh(d2.ret.loc[OOS[0]:OOS[1]])
    loco_streams[c] = d2
    for k in [k for k in cache if k[0] in ('u', 'w') and len(k) > 2 and k[2] == c]:
        del cache[k]
lc = min(loco, key=loco.get)
St = M.backtest(Wt, rank, band=band, cost_mult=1.5, lag=1, mmr=mmr, imr=imr)
Sc = M.backtest(Wt, rank, band=band, cost_mult=1.5, lag=0)
Sl = M.backtest(Wt, rank, band=band, cost_mult=1.0, lag=1)
rob = dict(lomo_min=lm, lomo_worst_month=lm_month, loco_min=loco[lc], loco_worst_coin=lc,
           loco_n=len(loco), loco_median=float(np.median(list(loco.values()))),
           stress_cost15_lag1=sh(St.ret.loc[OOS[0]:OOS[1]]), cost15_only=sh(Sc.ret.loc[OOS[0]:OOS[1]]),
           lag1_only=sh(Sl.ret.loc[OOS[0]:OOS[1]]))
res['tsmom']['robustness'] = rob
print('robustness', rob)

# executability: lot rounding for small accounts (today's OKX ctVal/lotSz/minSz on historical prices)
ex = {}
for E0 in [1000, 3000, 10000]:
    for L in [1]:
        d = M.backtest(Wt, rank, band=band, L=L, E0=E0, lots=(cv, minq), mmr=mmr, imr=imr)
        ex[f'E{E0}_L{L}'] = dict(sharpe_oos=sh(d.ret.loc[OOS[0]:OOS[1]] / L),
                                 gross_oos=float(d.gross.loc[OOS[0]:OOS[1]].mean()),
                                 sharpe_is=sh(d.ret.loc[M.IS_START:M.IS_END]))
res['tsmom']['executability'] = ex
print('exec', ex)

# leverage (linear scaling of the unit path; trough / maintenance / initial margin all scale with L)
tI = Tr.loc[M.IS_START:M.IS_END]
hk_t = kelly(lambda L: L * tI.values, tI)
To = T.loc[OOS[0]:OOS[1]]
lev_t = []
for L in LEVS:
    r = lev_sim(L * To.ret, L * To.ib, L * To.mm, L * To.im)
    lev_t.append(dict(stream='tsmom', L=L, **r))
res['tsmom']['kelly'] = hk_t
res['tsmom']['leverage'] = lev_t
res['tsmom']['supportable_L'] = supportable(lev_t, hk_t['half_kelly'])
print('kelly', hk_t, 'supportable', res['tsmom']['supportable_L'])
for r in lev_t:
    print(r)

# ------------------------------------------------------------------ (b) combination
B = pd.read_csv(BOOK, index_col=0, parse_dates=True)
B.index = B.index.tz_convert('UTC') if B.index.tz is not None else B.index.tz_localize('UTC')
Cy = pd.read_csv(f'{W}/carry_daily.csv', index_col=0, parse_dates=True)
Cy.index = Cy.index.tz_convert('UTC') if Cy.index.tz is not None else Cy.index.tz_localize('UTC')
idx = B.loc['2023-08-03':'2026-08-31'].index
b = B.loc[idx]
sig_b = b['return'].ewm(halflife=20, min_periods=10).std().shift(1).bfill()
book = pd.DataFrame({'ret': b['return'], 'cost': b.fees + b.spread + b.impact, 'gross': b.gross})
# book intrabar trough is not observable from its daily file: proxy = close-to-close loss minus one trailing daily sigma
book['ib'] = np.minimum(book.ret, 0) - sig_b
book['ib_harsh'] = 2 * np.minimum(book.ret, 0) - 2 * sig_b      # sensitivity: harsher proxy
book['mm'] = BOOK_MMR * book.gross
book['im'] = BOOK_IMR * book.gross
# latency stress for the book: its report gives Sharpe 1.363 -> 1.269 with one bar latency over 2023-08..2026-08;
# approximated as a constant daily drag of (1.363-1.269)*sigma/sqrt(365)
lat_drag = (1.363 - 1.269) * book.ret.std() / np.sqrt(365)
book['ret_stress'] = book.ret - 0.5 * book.cost - lat_drag
ts_ = T.reindex(idx)
carry = Cy.reindex(idx)


def combine(a, rb, rt, rc, bor, L, ibb=None, ibt=None, ibc=None, mmb=None, mmt=None, imb=None, imt=None):
    ab, at, ac = a
    ell = L * ac
    brw = np.maximum(ell - 1, 0) * bor
    r = L * (ab * rb + at * rt) + ell * rc - brw
    if ibb is None:
        return r
    ib = L * (ab * ibb + at * ibt) + ell * ibc - brw
    mm = L * (ab * mmb + at * mmt) + CARRY_MM_PER_L * ell + CARRY_BORROW_MMR * np.maximum(ell - 1, 0)
    im = L * (ab * imb + at * imt) + CARRY_IM_PER_L * ell + CARRY_BORROW_IMR * np.maximum(ell - 1, 0)
    return r, ib, mm, im


def weights(rb, rt, rc, scheme):
    v = np.array([rb.loc[C_IS[0]:C_IS[1]].std(), rt.loc[C_IS[0]:C_IS[1]].std(), rc.loc[C_IS[0]:C_IS[1]].std()])
    if scheme == 'inv_vol_3':
        a = 1 / v
    elif scheme == 'inv_var_3':
        a = 1 / v ** 2
    elif scheme == 'inv_vol_book_tsmom':
        a = np.array([1 / v[0], 1 / v[1], 0.0])
    elif scheme == 'equal_capital_3':
        a = np.ones(3)
    elif scheme == 'book_alone':
        a = np.array([1.0, 0, 0])
    elif scheme == 'tsmom_alone':
        a = np.array([0, 1.0, 0])
    return a / a.sum()


streams = pd.DataFrame({'book': book.ret, 'tsmom': ts_.ret, 'carry': carry.ret})
corr = {'is': streams.loc[C_IS[0]:C_IS[1]].corr().round(3).to_dict(),
        'oos': streams.loc[OOS[0]:OOS[1]].corr().round(3).to_dict(),
        'oos_weekly': (1 + streams.loc[OOS[0]:OOS[1]]).resample('W').prod().sub(1).corr().round(3).to_dict()}
res['streams'] = {k: dict(sharpe_cis=sh(streams[k].loc[C_IS[0]:C_IS[1]]), sharpe_oos=sh(streams[k].loc[OOS[0]:OOS[1]]),
                          vol_oos=float(streams[k].loc[OOS[0]:OOS[1]].std() * np.sqrt(365)),
                          ann_ret_oos=float(streams[k].loc[OOS[0]:OOS[1]].mean() * 365),
                          y2025=yret(streams[k], 2025), y2026=yret(streams[k], 2026)) for k in streams}
res['correlations'] = corr
print('streams', json.dumps(res['streams'], indent=1)); print('corr', json.dumps(corr, indent=1))

res['combos'] = {}
combo_daily = {}
lev_rows = [dict(r) for r in lev_t]
for scheme in ['inv_vol_3', 'inv_var_3', 'inv_vol_book_tsmom', 'equal_capital_3', 'book_alone', 'tsmom_alone']:
    a = weights(book.ret, ts_.ret, carry.ret, scheme)
    r1 = combine(a, book.ret, ts_.ret, carry.ret, carry.borrow_daily, 1)
    combo_daily[scheme] = r1
    o = r1.loc[OOS[0]:OOS[1]]
    ci = r1.loc[C_IS[0]:C_IS[1]]
    lm, lmm = lomo_min(r1)
    # leave-one-coin-out: TSMOM sleeve without coin c (weights re-estimated on IS); carry without BTC or ETH
    lc_vals = {}
    for c, d2 in loco_streams.items():
        rt2 = d2.ret.reindex(idx)
        a2 = weights(book.ret, rt2, carry.ret, scheme)
        lc_vals[c] = sh(combine(a2, book.ret, rt2, carry.ret, carry.borrow_daily, 1).loc[OOS[0]:OOS[1]])
    for c, col in [('BTCUSDT', 'ret_eth'), ('ETHUSDT', 'ret_btc')]:
        rt2 = loco_streams[c].ret.reindex(idx) if c in loco_streams else ts_.ret
        a2 = weights(book.ret, rt2, carry[col], scheme)
        lc_vals[c] = sh(combine(a2, book.ret, rt2, carry[col], carry.borrow_daily, 1).loc[OOS[0]:OOS[1]])
    lcw = min(lc_vals, key=lc_vals.get)
    rs = combine(a, book.ret_stress, St.ret.reindex(idx), carry.ret_cost15, carry.borrow_daily, 1)
    # leverage
    cI = (book.ret.loc[C_IS[0]:C_IS[1]], ts_.ret.loc[C_IS[0]:C_IS[1]], carry.ret.loc[C_IS[0]:C_IS[1]],
          carry.borrow_daily.loc[C_IS[0]:C_IS[1]])
    hk = kelly(lambda L: combine(a, *cI, L).values, ci)
    lev = []
    O = slice(OOS[0], OOS[1])
    for L in LEVS:
        r, ib, mm, im = combine(a, book.ret[O], ts_.ret[O], carry.ret[O], carry.borrow_daily[O], L,
                                book.ib[O], ts_.ib[O], carry.ib[O], book.mm[O], ts_.mm[O], book.im[O], ts_.im[O])
        x = lev_sim(r, ib, mm, im)
        gr = L * (a[0] * book.gross[O] + a[1] * ts_.gross[O]) + 2 * L * a[2]
        x.update(sharpe_oos_at_L=sh(r), gross_avg=float(gr.mean()), gross_max=float(gr.max()))
        _, ibh, _, _ = combine(a, book.ret[O], ts_.ret[O], carry.ret[O], carry.borrow_daily[O], L,
                               book.ib_harsh[O], ts_.ib[O], carry.ib[O], book.mm[O], ts_.mm[O], book.im[O], ts_.im[O])
        xh = lev_sim(r, ibh, mm, im)
        x.update(maxdd_intrabar_harsh_book_proxy=xh['maxdd_intrabar'], liquidated_harsh=xh['liquidated'])
        lev.append(dict(L=L, **x))
        lev_rows.append(dict(stream=f'combo_{scheme}', L=L, **{k: v for k, v in x.items()}))
    sup = supportable(lev, hk['half_kelly'])
    sup_h = supportable([dict(r, maxdd_intrabar=r['maxdd_intrabar_harsh_book_proxy'], liquidated=r['liquidated_harsh'])
                         for r in lev], hk['half_kelly'])
    res['combos'][scheme] = dict(
        weights=dict(zip(['book', 'tsmom', 'carry'], a.round(4).tolist())),
        sharpe_cis=sh(ci), sharpe_oos=sh(o), cagr_oos=float((1 + o).prod() ** (365.25 / len(o)) - 1),
        vol_oos=float(o.std() * np.sqrt(365)), y2025=yret(r1, 2025), y2026=yret(r1, 2026),
        lomo_min=lm, lomo_worst_month=lmm, loco_min=lc_vals[lcw], loco_worst_coin=lcw,
        stress_cost15_lag1=sh(rs.loc[OOS[0]:OOS[1]]), kelly=hk, leverage=lev, supportable_L=sup,
        supportable_L_harsh_book_proxy=sup_h, loco_note='book sleeve cannot be re-run without a coin (daily returns only)')
    print(scheme, json.dumps({k: v for k, v in res['combos'][scheme].items() if k != 'leverage'}, default=str))
    for x in lev:
        print('   ', x)

pd.DataFrame(combo_daily).assign(book=book.ret, tsmom=ts_.ret, carry=carry.ret).to_csv(f'{W}/combo_daily.csv')
T.to_csv(f'{W}/tsmom_selected_daily.csv')
pd.DataFrame(lev_rows).to_csv(f'{W}/leverage_table.csv', index=False)
pd.Series(loco).to_csv(f'{W}/tsmom_loco.csv')
json.dump(res, open(f'{W}/results.json', 'w'), indent=1, default=str)
print('saved')

# ------------------------------------------------------------------ (c) sensitivities
# (c1) the book's intrabar path is unobservable; on 2025-10-10 (market-wide wick: OKX mark lows LTC -59 %, DOGE -65 %,
# BNB -31 %) it had gross 1.32 and 13 catastrophe stops. Scenario: an extra unit-level intrabar loss X on that day.
O = slice(OOS[0], OOS[1])
sens = {}
for scheme in ['inv_vol_3', 'inv_vol_book_tsmom', 'equal_capital_3', 'book_alone']:
    a = weights(book.ret, ts_.ret, carry.ret, scheme)
    hk = res['combos'][scheme]['kelly']['half_kelly']
    for X in [0.05, 0.10, 0.20]:
        ibb = book.ib.copy()
        ibb.loc['2025-10-10'] = ibb.loc['2025-10-10'] - X
        lev = []
        for L in LEVS:
            r, ib, mm, im = combine(a, book.ret[O], ts_.ret[O], carry.ret[O], carry.borrow_daily[O], L,
                                    ibb[O], ts_.ib[O], carry.ib[O], book.mm[O], ts_.mm[O], book.im[O], ts_.im[O])
            lev.append(dict(L=L, **lev_sim(r, ib, mm, im)))
        sens[f'{scheme}_book1010_minus{int(X * 100)}pct'] = dict(
            supportable_L=supportable(lev, hk), maxdd_by_L={x['L']: round(x['maxdd_intrabar'], 3) for x in lev},
            liq_by_L={x['L']: x['liquidated'] for x in lev})
res['sensitivity_book_intrabar_20251010'] = sens
print(json.dumps(sens, indent=0))

# (c2) combination across the whole TSMOM grid (72 configs): how much does the result depend on the TSMOM choice?
Gd = pd.read_csv(f'{W}/grid_tsmom_daily.csv', index_col=0, parse_dates=True)
Gd.index = Gd.index.tz_convert('UTC') if Gd.index.tz is not None else Gd.index.tz_localize('UTC')
rows = []
for name in Gd.columns:
    rt = Gd[name].reindex(idx)
    for scheme in ['inv_vol_3', 'inv_vol_book_tsmom', 'equal_capital_3']:
        a = weights(book.ret, rt, carry.ret, scheme)
        r1 = combine(a, book.ret, rt, carry.ret, carry.borrow_daily, 1)
        rows.append(dict(tsmom=name, scheme=scheme, sharpe_cis=sh(r1.loc[C_IS[0]:C_IS[1]]),
                         sharpe_oos=sh(r1.loc[O]), y2025=yret(r1, 2025), y2026=yret(r1, 2026),
                         tsmom_sharpe_oos=sh(Gd[name].loc[O])))
GC = pd.DataFrame(rows)
GC.to_csv(f'{W}/grid_combo.csv', index=False)
summ = GC.groupby('scheme').agg(n=('sharpe_oos', 'size'), oos_min=('sharpe_oos', 'min'),
                                oos_median=('sharpe_oos', 'median'), oos_max=('sharpe_oos', 'max'),
                                frac_ge_1_5=('sharpe_oos', lambda x: float((x >= 1.5).mean())),
                                frac_2026_pos=('y2026', lambda x: float((x > 0).mean())),
                                frac_item1=('sharpe_oos', lambda x: 0.0))
for s_ in summ.index:
    g = GC[GC.scheme == s_]
    summ.loc[s_, 'frac_item1'] = float(((g.sharpe_oos >= 1.5) & (g.y2025 > 0) & (g.y2026 > 0)).mean())
print(summ.to_string())
res['combo_over_tsmom_grid'] = summ.reset_index().to_dict(orient='records')
json.dump(res, open(f'{W}/results.json', 'w'), indent=1, default=str)
print('saved2')

# (c3) carry sleeve with OKX funding instead of Binance funding (OKX static monthly files, BTC/ETH):
# daily adjustment of the 1x carry stream = 0.5 * sum_coin (okx - binance) (short perp leg receives funding)
fx = pd.read_csv(f'{W}/okx_funding_btceth.csv', index_col=0, parse_dates=True)
fx.index = fx.index.tz_convert('UTC') if fx.index.tz is not None else fx.index.tz_localize('UTC')
adj = (0.5 * ((fx.okx_BTC - fx.bin_BTC) + (fx.okx_ETH - fx.bin_ETH))).reindex(idx).fillna(0.0)
c_okx = carry.ret + adj
okxf = {'carry_alone_oos_ann_binance': float(carry.ret[O].mean() * 365), 'carry_alone_oos_ann_okx': float(c_okx[O].mean() * 365),
        'carry_alone_sharpe_oos_okx': sh(c_okx[O])}
for scheme in ['inv_vol_3', 'inv_var_3', 'equal_capital_3']:
    a = weights(book.ret, ts_.ret, c_okx, scheme)
    r1 = combine(a, book.ret, ts_.ret, c_okx, carry.borrow_daily, 1)
    cI = (book.ret.loc[C_IS[0]:C_IS[1]], ts_.ret.loc[C_IS[0]:C_IS[1]], c_okx.loc[C_IS[0]:C_IS[1]],
          carry.borrow_daily.loc[C_IS[0]:C_IS[1]])
    hk = kelly(lambda L: combine(a, *cI, L).values, r1.loc[C_IS[0]:C_IS[1]])
    lev = []
    for L in LEVS:
        r, ib, mm, im = combine(a, book.ret[O], ts_.ret[O], c_okx[O], carry.borrow_daily[O], L,
                                book.ib[O], ts_.ib[O], carry.ib[O], book.mm[O], ts_.mm[O], book.im[O], ts_.im[O])
        x = lev_sim(r, ib, mm, im)
        lev.append(dict(L=L, **x))
    sup = supportable(lev, hk['half_kelly'])
    okxf[scheme] = dict(weights=a.round(4).tolist(), sharpe_oos=sh(r1[O]), y2025=yret(r1, 2025), y2026=yret(r1, 2026),
                        supportable_L=sup, cagr_oos_at_supportable=[x['cagr'] for x in lev if x['L'] == sup][0] if sup else None,
                        cagr_oos_by_L={x['L']: round(x['cagr'], 4) for x in lev})
res['carry_okx_funding'] = okxf
print(json.dumps(okxf, indent=0))
json.dump(res, open(f'{W}/results.json', 'w'), indent=1, default=str)
print('saved3')
