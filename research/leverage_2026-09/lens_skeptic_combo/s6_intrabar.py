"""Supportable combined scale L under haircuts, with the listing sleeve's hourly intrabar path.
For each L: listing sim (hybrid, selected cfg) at coin cap w_l*L on the OOS -> daily close returns and the daily
intrabar trough (min over hours of worst-case equity / previous day close - 1; shorts at bar highs, hedge at adverse
extreme). Book: daily return x L*w_b; intraday trough 'close' (= its daily loss) or 'stress' (2x its daily loss plus
0.5% per unit of average gross, a crude allowance; the book's own intraday path is not saved). Daily rebalanced weights.
Haircuts remove drift in proportion to each sleeve's gross (k from s1 / s3). Also: listing alone at coin cap w_l*L
(the book has been flat since 2026-03, so this is what the combination is today).
-> s6_intrabar.json, s6_intrabar.csv"""
import json, os, sys, numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'newlisting'))
import sim
from s0_repro import load, sharpe, cagr, maxdd, years

cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
Dt = sim.Data('hybrid')
j = load().dropna(subset=['book', 'nl'])['2025-01-01':'2026-08-31']
G = pd.read_csv(os.path.join(HERE, 'nl_gross_daily.csv'), index_col=0, parse_dates=True).reindex(j.index)
S1 = json.load(open(os.path.join(HERE, 's1_haircut.json'))); KB = S1['scenarios_k']
S3 = json.load(open(os.path.join(HERE, 's3_regime.json'))); KL = S3['k_nl']
W = 0.6030102397652416; WL = 1 - W
LEVS = [1, 2, 2.5, 3, 3.5, 4, 5, 6]
gb_avg = float(j.book_gross.mean())
LS = {}
for L in LEVS:
    r = sim.simulate(Dt, cfg, '2025-01-01', '2026-09-01', L=WL * L, record=True)
    eqc = r['eq'].resample('D').last(); prev = eqc.shift(1).fillna(1.0)
    trough = (r['eq_worst'].resample('D').min() / prev - 1).fillna(0.0)
    LS[L] = dict(ret=r['ret'].reindex(j.index), trough=np.minimum(trough.reindex(j.index), r['ret'].reindex(j.index)), liq=r['liq'],
                 ib_dd_alone=r['maxdd'], ddd_alone=maxdd(r['ret']), cagr_alone=cagr(r['ret']), sh_alone=sharpe(r['ret']))

def path(rc, rw):
    """rc: daily close returns; rw: daily intrabar trough returns (<= rc). -> intrabar max DD, daily max DD"""
    C = np.cumprod(1 + rc.values); prevC = np.r_[1.0, C[:-1]]
    peak_prev = np.maximum.accumulate(np.r_[1.0, C])[:-1]
    worst = prevC * (1 + rw.values)
    ib = float(np.max(1 - worst / peak_prev)); dd = maxdd(rc)
    return max(ib, dd), dd, float(np.min(worst / prevC - 1))

rows = []
SCEN = [('as_is', 'as_is_1.40'), ('haircut50_0.68', 'as_is_1.40'), ('as_is', 'grid_short_median_1.11'),
        ('haircut50_0.68', 'grid_short_median_1.11'), ('dsr_excess', 'as_is_1.40'), ('dsr_excess', 'grid_short_median_1.11'),
        ('haircut50_0.68', 'half_0.70'), ('zero', 'as_is_1.40'), ('as_is', 'zero')]
for bn, ln in SCEN:
    for bm in ('close', 'stress'):
        for L in LEVS:
            rb = j.book - KB[bn] * j.book_gross
            adj_l = KL[ln] * G.nl_gross * WL * L
            rl = LS[L]['ret'] - adj_l; tl = LS[L]['trough'] - adj_l
            bt = np.minimum(rb, 0) if bm == 'close' else 2 * np.minimum(rb, 0) - 0.005 * j.book_gross / gb_avg
            rc = L * W * rb + rl
            rw = np.minimum(L * W * bt + tl, rc)
            ib, dd, wd = path(rc, rw)
            rows.append(dict(book=bn, listing=ln, book_intraday=bm, L=L, sharpe=sharpe(rc), cagr=cagr(rc), y2025=years(rc)[2025],
                             y2026=years(rc)[2026], dd_daily=dd, dd_intrabar=ib, worst_intraday_loss=wd, listing_liq=LS[L]['liq'],
                             half_kelly_oos=float(0.5 * rc.mean() / rc.var()) * L))
df = pd.DataFrame(rows)
df.to_csv(os.path.join(HERE, 's6_intrabar.csv'), index=False)
pd.set_option('display.width', 250)
piv = df.pivot_table(index=['book', 'listing', 'book_intraday'], columns='L', values='dd_intrabar').round(3)
print(piv.to_string())
sup = {}
for (bn, ln, bm), g in df.groupby(['book', 'listing', 'book_intraday']):
    ok = g[(g.dd_intrabar <= 0.35) & (~g.listing_liq) & (g.L <= g.half_kelly_oos)]
    sup[f'{bn}|{ln}|{bm}'] = dict(L_max=float(ok.L.max()) if len(ok) else 0.0,
                                   cagr_at_Lmax=float(ok.sort_values('L').cagr.iloc[-1]) if len(ok) else None,
                                   sharpe=float(g.sharpe.iloc[0]))
print(json.dumps(sup, indent=1))
alone = {str(L): dict(coin_cap=WL * L, total_gross_cap=2 * WL * L, ib_dd=v['ib_dd_alone'], dd_daily=v['ddd_alone'], cagr=v['cagr_alone'],
                      sharpe=v['sh_alone'], liq=v['liq']) for L, v in LS.items()}
print('listing alone at coin cap w_l*L:', json.dumps({k: {kk: round(vv, 3) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in alone.items()}))
# exchange-gross of the combination at each L
eg = {str(L): dict(mean=float((L * W * j.book_gross + G.nl_gross * WL * L).mean()),
                   p99=float((L * W * j.book_gross + G.nl_gross * WL * L).quantile(0.99)),
                   max_cap=float(L * W * j.book_gross.max() + 2 * WL * L)) for L in LEVS}
print('exchange gross:', json.dumps(eg))
json.dump(dict(supportable=sup, listing_alone=alone, exchange_gross=eg, rows=rows), open(os.path.join(HERE, 's6_intrabar.json'), 'w'), indent=1, default=float)
