import time, sys
sys.path.insert(0, '.')
from pairs_bt import *
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64)
day_all = GRID.normalize()
for scheme in [('corr','ret',120,5), ('fixed','ret',120,5), ('coint','lvl',60,5)]:
    method, hedge, Wf, K = scheme
    Wz=168
    PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[(method, hedge, Wf)], K, Wz, base_slip)
    for pname,(p0,p1) in {'IS':(IS0,IS1),'OOS':(OOS0,OOS1)}.items():
        i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
        days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
        DAY = ((day_all - p0).days).values.astype(np.int64)
        for fee in [0.0, FEE_T]:
          for lev in [1, 5, 20]:
            t=time.time()
            daily, st, TR = sim(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, mmr, imr,
                            2.0, 0.0, 4.0, Wz, float(lev), False, fee, RANGE_SLIP if fee>0 else 0.0, 100000)
            m = metrics(daily, days)
            print(scheme, pname, 'fee', fee, 'L', lev, f"cagr {m['cagr']:.3f} sh {m['sharpe']:.2f} dd {st[1]:.3f} tr {int(st[2])} liq {int(st[3])} stop {int(st[4])} time {int(st[5])} force {int(st[6])} fees {st[7]:.3f} fund {st[8]:.3f} expo {st[9]:.2f} gl {st[10]:.2f} {time.time()-t:.2f}s")
            if lev==1 and fee>0:
                tr = pd.DataFrame(TR, columns=['k','a','b','t0','t1','pnl','why','dir','basis'])
                print('  trades', len(tr), 'mean pnl/eq %.5f' % tr.pnl.mean(), 'win %.2f' % (tr.pnl>0).mean(), tr.groupby('why').pnl.agg(['count','mean','sum']).round(4).to_dict())
