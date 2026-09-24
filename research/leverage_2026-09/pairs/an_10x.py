"""Trade-level look at the IS-selected 10x config (m1 bound needs 1m data: here h1 is used for the trade list;
the h1 and m1 runs of this config both show 0 liquidations, so the trade list is identical)."""
import sys
sys.path.insert(0, '.')
from pairs_v2 import *
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64)
K = 10
PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[('coint', 'ret', 60)], K, 72, base_slip)
W0 = np.full((K, NB), np.nan)
for pname, (p0, p1) in {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}.items():
    i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
    DAY = ((GRID.normalize() - p0).days).values.astype(np.int64)
    for lev in [1.0, 10.0]:
        daily, st, TR = sim2(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, W0, W0, mmr, imr,
                             3.0, 0.5, 4.5, 72, lev, False, FEE_T, FEE_M, RANGE_SLIP, True, False, 100000)
        tr = pd.DataFrame(TR, columns=['k', 'a', 'b', 't0', 't1', 'pnl', 'why', 'dir', 'basis'])
        tr['pair'] = [f'{syms[int(a)]}/{syms[int(b)]}' for a, b in zip(tr.a, tr.b)]
        tr['t0'] = GRID[tr.t0.astype(int)]
        lr = np.log1p(tr.pnl.clip(lower=-0.999))
        m = metrics(daily, days)
        print(pname, lev, 'cagr %.3f final %.3f dd %.3f trades %d liq %d' % (m['cagr'], m['final'], st[1], st[2], st[3]),
              'sum log-ret %.3f, top-5 trades %.3f, top-10 %.3f' % (lr.sum(), lr.nlargest(5).sum(), lr.nlargest(10).sum()))
        if lev == 10.0:
            print(tr.sort_values('pnl').iloc[list(range(3)) + list(range(-5, 0))][['pair', 't0', 'dir', 'why', 'pnl']].to_string(index=False))
            e = pd.Series(daily, index=days)
            print('equity by quarter:', e.resample('QE').last().round(3).to_dict())
