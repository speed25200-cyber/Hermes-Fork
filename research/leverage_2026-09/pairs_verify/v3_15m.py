"""Verifier step 3: reproduce the 15m 1x IS pick (coint/ret/Wf60 K3 Wz672 zin2.5 zout0.5 no stop, maker) from raw
Binance 15m klines (in memory), then stress it: maker trade-through 5/10 bp, leave-one-coin-out, leave-best-month-out."""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import pairs_bt as pb
import pairs_15m as p15
from pairs_v2 import FEE_M
from sim3 import sim3
P, syms, first_bar, U, mmr, imr = pb.prepare()
sel = pb.build_selections(P, U, first_bar)
KEY = ('coint', 'ret', 60); K = 3; SG = (672, 2.5, 0.5, 101.5)
sub = {KEY: {t: v[:K] for t, v in sel[KEY].items()}}
t = time.time()
A = p15.load15(list(P['syms']), sel)          # exactly as pairs_15m.py (all selections, top 10, month + previous)
print('15m loaded', f'{time.time()-t:.0f}s', flush=True)
# months after a selected month (forced exits at the next month's first bar) that the original loader did not fetch
extra = set()
for t_, v in sub[KEY].items():
    for (a_, b_, be_) in v:
        for s_ in (a_, b_):
            m1_ = (pd.Timestamp(t_) + pd.offsets.MonthBegin(1))
            if m1_ < pb.G1:
                i0_ = p15.GRID.get_loc(m1_)
                if np.isnan(A['o'][s_, i0_]):
                    extra.add((s_, m1_.strftime('%Y-%m')))
print('next-month symbol-months missing at forced exits:', len(extra), flush=True)
EXTRA = {}
for (s_, m_) in sorted(extra):
    df_ = p15.fetch15(list(P['syms'])[s_], m_)
    if df_ is not None:
        EXTRA[(s_, m_)] = df_
FR = p15.fund15(P)
names = list(P['syms'])
NB = p15.NB; GRID = p15.GRID
import numpy as np, pandas as pd
O = A['o'].astype(np.float64); H = A['h'].astype(np.float64); L = A['l'].astype(np.float64); C = A['c'].astype(np.float64)
del A, P
logc = np.log(C)
day_all = GRID.normalize()
W0 = np.full((K, NB), np.nan)
g = pd.read_csv('../pairs/out/grid_15m.csv.gz')

def run(ex, lev, period, tick=1e-4, s_m=None, rec=0):
    PA, PB, BE, Z, SA, SB, NEWM = p15.slot15(logc, U, names, s_m or sub[KEY], K, SG[0])
    p0, p1 = {'IS': (pb.IS0, pb.IS1), 'OOS': (pb.OOS0, pb.OOS1)}[period]
    i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < pb.G1 else NB
    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
    DAY = ((day_all - p0).days).values.astype(np.int64)
    daily, st, TR = sim3(O, H, L, C, H, L, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, W0, W0, mmr, imr,
                         SG[1], SG[2], SG[3], SG[0], float(lev), False, pb.FEE_T, FEE_M if ex == 'maker' else pb.FEE_T,
                         pb.RANGE_SLIP, ex == 'maker', False, rec, tick, 0)
    return pb.metrics(daily, days), st, TR, pd.Series(daily, index=days)

rows = []
for period in ['IS', 'OOS']:
    for ex in ['maker', 'taker']:
        for lev in pb.LEVS:
            for tick in ([1e-4, 5e-4, 1e-3] if ex == 'maker' else [1e-4]):
                m, st, TR, d = run(ex, lev, period, tick)
                q = g[(g.method == KEY[0]) & (g.hedge == KEY[1]) & (g.Wf == KEY[2]) & (g.K == K) & (g.Wz == SG[0]) &
                      (g.zin == SG[1]) & (g.zout == SG[2]) & (g.zstop == SG[3]) & (g.exec == ex) & (g.lev == lev) & (g.period == period)]
                rows.append(dict(exec=ex, lev=lev, period=period, tick_bp=tick * 1e4, cagr=m['cagr'],
                                 cagr_reported=float(q.cagr.iloc[0]) if tick == 1e-4 and len(q) else np.nan,
                                 maxdd=st[1], worst_day=m['worst_day'], sharpe=m['sharpe'], liqs=int(st[3]), trades=int(st[2]),
                                 legged=int(st[11]), maker_miss=int(st[12]),
                                 per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()})))
R = pd.DataFrame(rows)
pd.set_option('display.width', 250)
print('=== reproduction (original data loading)')
print(R.round(4).to_string(index=False))
R.to_csv('out/v3_15m_runs_orig.csv', index=False)
t0_ = GRID[0].value // 10**6
nfill = 0
for (s_, m_), df_ in EXTRA.items():
    idx = ((df_.t.values - t0_) // 900000).astype(np.int64); ok = (idx >= 0) & (idx < NB)
    for arr, col in ((O, 'o'), (H, 'h'), (L, 'l'), (C, 'c')):
        cur = arr[s_, idx[ok]]; newv = df_[col].values[ok]
        arr[s_, idx[ok]] = np.where(np.isnan(cur), newv, cur)
    nfill += int(ok.sum())
logc[:] = np.log(C)
print('filled bars', nfill, flush=True)
rows = []
for period in ['IS', 'OOS']:
    for ex in ['maker', 'taker']:
        for lev in pb.LEVS:
            for tick in ([1e-4, 5e-4, 1e-3] if ex == 'maker' else [1e-4]):
                m, st, TR, d = run(ex, lev, period, tick)
                rows.append(dict(exec=ex, lev=lev, period=period, tick_bp=tick * 1e4, cagr=m['cagr'], maxdd=st[1],
                                 worst_day=m['worst_day'], sharpe=m['sharpe'], liqs=int(st[3]), trades=int(st[2]),
                                 legged=int(st[11]), maker_miss=int(st[12]),
                                 per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()})))
R = pd.DataFrame(rows)
print('=== with next-month data for forced exits (corrected)')
print(R.round(4).to_string(index=False))
R.to_csv('out/v3_15m_runs.csv', index=False)
# leave-one-coin-out and best-month-out, OOS, maker 1x
m, st, TR, d = run('maker', 1, 'OOS', rec=5000)
mo = d.ffill().resample('ME').last(); mr = mo.pct_change(); mr.iloc[0] = mo.iloc[0] - 1
print('OOS monthly returns (15m pick, maker 1x):'); print(mr.round(4).to_string())
yrs = len(d) / 365.25
for drop in [1, 2]:
    keep = mr.drop(mr.nlargest(drop).index)
    print(f'drop best {drop} month(s): CAGR over remaining', round(float(np.prod(1 + keep) ** (12 / len(keep)) - 1), 4))
coins = sorted({s for t, v in sub[KEY].items() if t >= pb.OOS0 - pd.offsets.MonthBegin(1) for (a, b, be) in v for s in (a, b)})
loo = []
for c in coins:
    s2 = {t: [p for p in v if c not in p[:2]] for t, v in sub[KEY].items()}
    m2, st2, _, _ = run('maker', 1, 'OOS', s_m=s2)
    loo.append(dict(coin=names[c], cagr_oos=m2['cagr'], trades=int(st2[2])))
loo = pd.DataFrame(loo).sort_values('cagr_oos')
print(loo.round(4).to_string(index=False))
print('LOO: n', len(loo), 'min', loo.cagr_oos.min().round(4), 'median', loo.cagr_oos.median().round(4), 'n<=0', int((loo.cagr_oos <= 0).sum()))
loo.to_csv('out/v3_15m_loo.csv', index=False)
