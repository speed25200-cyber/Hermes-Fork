"""(b) Per-year / per-half-year Sharpe 2022..2026 and alt bull/bear regime split (sign of the EW alt index 30-day
return known at the start of the day / at trade entry), for the selected config and the ensembles; plus an
event-level study independent of portfolio mechanics: every new-token Binance listing that OKX lists at +d0,
coin return from open(t0+d0) to open(t0+168h), raw / minus BTC / minus alt index. -> regime.json"""
import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
E = pd.read_parquet('ens_daily.parquet')
A = pd.read_parquet('altidx_d.parquet').loc[:'2026-08-31']
Ah = pd.read_parquet('altidx_h.parquet')
def sh(x):
    x = np.asarray(x, float); s = x.std(ddof=1); return float(x.mean() / s * np.sqrt(365)) if len(x) > 2 and s > 0 else float('nan')
names = ['selected (IS rank 1)', 'IS top-10', 'IS top-20', 'plateau d0 in {1d,3d} -> d7, all beta/stop/uni (32)',
         'plateau, BTC-hedged only (16)', 'all short configs, d1<=14d', 'all short configs (IS-eligible)']
out = {}
half = lambda idx: [f"{y}H{1 if m <= 6 else 2}" for y, m in zip(idx.year, idx.month)]
for n in names:
    r = E[n]
    hy = r.groupby(half(r.index))
    yy = r.groupby(r.index.year)
    reg = A.ret30_known.reindex(r.index)
    d = dict(half_sharpe={k: sh(v) for k, v in hy}, half_ret={k: float(np.prod(1 + v) - 1) for k, v in hy},
             year_sharpe={str(k): sh(v) for k, v in yy})
    for per, sl in (('IS', slice('2022-01-01', '2024-12-31')), ('OOS', slice('2025-01-01', '2026-08-31')), ('ALL', slice(None))):
        rr, gg = r.loc[sl], reg.loc[sl]
        d[f'{per}_bull_sharpe'] = sh(rr[gg > 0]); d[f'{per}_bear_sharpe'] = sh(rr[gg <= 0])
        d[f'{per}_bull_ann_mean'] = float(rr[gg > 0].mean() * 365); d[f'{per}_bear_ann_mean'] = float(rr[gg <= 0].mean() * 365)
        d[f'{per}_bull_days'] = int((gg > 0).sum()); d[f'{per}_bear_days'] = int((gg <= 0).sum())
    out[n] = d
    print(n); print('  half Sharpe', {k: round(v, 2) for k, v in d['half_sharpe'].items()})
    print('  half ret   ', {k: round(v, 3) for k, v in d['half_ret'].items()})
    print('  bull/bear Sharpe IS %.2f/%.2f OOS %.2f/%.2f ALL %.2f/%.2f  ann mean ALL %.3f/%.3f days %d/%d' % (
        d['IS_bull_sharpe'], d['IS_bear_sharpe'], d['OOS_bull_sharpe'], d['OOS_bear_sharpe'], d['ALL_bull_sharpe'], d['ALL_bear_sharpe'],
        d['ALL_bull_ann_mean'], d['ALL_bear_ann_mean'], d['ALL_bull_days'], d['ALL_bear_days']))
# fraction of days in bull regime per half
reg = A.ret30_known
print('alt bull-day fraction by half', {k: round(float((v > 0).mean()), 2) for k, v in reg.loc['2022-01-01':].groupby(half(reg.loc['2022-01-01':].index))})
# ---- event level
Dt = Data('hybrid')
ai = ((Dt.ev.t0.values - G0) // HMS).astype(np.int64)
t_idx = Ah.index.values
aio = Ah.o.values
pos0 = np.searchsorted(t_idx, G0)
rows = []
for d0 in (24, 72):
    d1 = 168
    for i in range(Dt.n):
        if not Dt.newtok[i] or not Dt.okx_on[i, d0]:
            continue
        po, pe = Dt.o[i, d0], Dt.o[i, d1]
        if not (np.isfinite(po) and np.isfinite(pe)):
            # delisted/halted within the window: use last close
            cc = Dt.c[i, d0:d1]; cc = cc[np.isfinite(cc)]
            if not np.isfinite(po) or len(cc) == 0:
                continue
            pe = cc[-1]
        b = Dt.bo[i, d1] / Dt.bo[i, d0] - 1
        ga, gb = pos0 + ai[i] + d0, pos0 + ai[i] + d1
        a = aio[gb] / aio[ga] - 1
        hi = np.nanmax(Dt.h[i, d0:d1])
        t_ent = pd.Timestamp(int(Dt.ev.t0.values[i]) + d0 * HMS, unit='ms')
        rg = A.ret30_known.get(t_ent.normalize(), np.nan)
        rows.append(dict(sym=Dt.ev.sym.values[i], d0=d0, t=t_ent, coin=pe / po - 1, btc=b, alt=a, mae=hi / po - 1, alt30=rg))
X = pd.DataFrame(rows)
X['half'] = half(pd.DatetimeIndex(X.t)); X['year'] = X.t.dt.year
X['short_btc'] = -(X.coin - X.btc); X['short_alt'] = -(X.coin - X.alt); X['short_raw'] = -X.coin
X.to_parquet('events_d7.parquet')
def tstat(v):
    v = np.asarray(v, float); return float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))) if len(v) > 2 else float('nan')
ev_out = {}
for d0 in (24, 72):
    Y = X[X.d0 == d0]
    for grp in ('year', 'half'):
        tab = Y.groupby(grp).agg(n=('coin', 'size'), raw=('short_raw', 'mean'), vs_btc=('short_btc', 'mean'), vs_alt=('short_alt', 'mean'),
                                 med_vs_btc=('short_btc', 'median'), win_vs_btc=('short_btc', lambda v: (v > 0).mean()),
                                 t_vs_btc=('short_btc', tstat), t_vs_alt=('short_alt', tstat))
        ev_out[f'd0={d0}_{grp}'] = tab.reset_index().astype({grp: str}).to_dict('records')
        print(f'\nEVENTS short from +{d0}h to +168h, by {grp}'); print(tab.round(3).to_string())
    for per, m in (('IS', Y.t < '2025-01-01'), ('OOS', Y.t >= '2025-01-01')):
        Z = Y[m]
        for nm, mm in (('bull', Z.alt30 > 0), ('bear', Z.alt30 <= 0)):
            W = Z[mm]
            ev_out[f'd0={d0}_{per}_{nm}'] = dict(n=int(len(W)), vs_btc=float(W.short_btc.mean()), vs_alt=float(W.short_alt.mean()), raw=float(W.short_raw.mean()),
                                               t_vs_btc=tstat(W.short_btc), med_vs_btc=float(W.short_btc.median()))
            print(f'd0={d0} {per} {nm}: n={len(W)} mean short vs BTC {W.short_btc.mean():+.4f} (t {tstat(W.short_btc):.2f}) vs alt {W.short_alt.mean():+.4f} raw {W.short_raw.mean():+.4f} median vsBTC {W.short_btc.median():+.4f}')
json.dump(dict(portfolio=out, events=ev_out), open('regime.json', 'w'), indent=1, default=str)
