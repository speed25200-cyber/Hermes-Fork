import sys
sys.path.insert(0, '.')
from pairs_bt import *
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64)
day_all = GRID.normalize()
PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[('coint','ret',120)], 3, 168, base_slip)
p0,p1=OOS0,OOS1
i0 = GRID.get_loc(p0); i1 = NB
days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
DAY = ((day_all - p0).days).values.astype(np.int64)
daily, st, TR = sim(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, mmr, imr, 2.0, 0.0, 101.0, 168, 1.0, False, FEE_T, RANGE_SLIP, 100000)
tr = pd.DataFrame(TR, columns=['k','a','b','t0','t1','pnl','why','dir','basis'])
print(tr.tail(5))
li = int(tr.t1.iloc[-1]); print('liq bar', GRID[li])
for k in range(3):
    a, b = PA[k, li], PB[k, li]
    print(k, syms[a], syms[b], BE[k, li])
    for s in [a, b]:
        print('  ', syms[s], 'o h l c', O[s, li-2:li+2], H[s, li-2:li+2], Lw[s, li-2:li+2], 'mh ml', MH[s, li-2:li+2], ML[s, li-2:li+2])
