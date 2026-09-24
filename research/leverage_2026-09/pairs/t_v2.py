"""Regression + sanity: sim2(taker, h1) vs sim at 1x; bounds for the fixed scheme only; maker vs taker."""
import sys, time
sys.path.insert(0, '.')
from pairs_v2 import *
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64)
day_all = GRID.normalize()
key = ('fixed', 'ret', 120); K = 5
pm = [(t, a, b, be) for (t, a, b, be) in all_pair_months({key: sel[key]}) if t >= pd.Timestamp('2025-09-01') and t <= pd.Timestamp('2025-10-01')]
t0 = time.time()
bounds = minute_bound.compute_bounds(sorted(pm), list(P['syms']), O, GRID, FORM_DATES)
print('bounds', len(bounds), time.time() - t0)
WLa, WSa = w_arrays(sel[key], K, bounds)
PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[key], K, 168, base_slip)
i = GRID.get_loc(pd.Timestamp('2025-10-10 21:00'))
for k in range(K):
    if PA[k, i] >= 0:
        a, b = PA[k, i], PB[k, i]
        print(syms[a], syms[b], 'beta %.2f' % BE[k, i], 'WL 1m %.4f WS 1m %.4f' % (WLa[k, i], WSa[k, i]),
              'hourly long-spread bound %.4f' % ((ML[a, i] / O[a, i] - 1) - BE[k, i] * (MH[b, i] / O[b, i] - 1)),
              'short %.4f' % (-(MH[a, i] / O[a, i] - 1) + BE[k, i] * (ML[b, i] / O[b, i] - 1)))
for pname, (p0, p1) in {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}.items():
    i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
    DAY = ((day_all - p0).days).values.astype(np.int64)
    d1, s1, _ = sim(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, mmr, imr, 2.0, 0.0, 5.0, 168, 1.0, False, FEE_T, RANGE_SLIP, 1)
    d2, s2, _ = sim2(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, WLa, WSa, mmr, imr, 2.0, 0.0, 5.0, 168, 1.0, False, FEE_T, FEE_M, RANGE_SLIP, False, False, 1)
    print(pname, 'sim final %.5f tr %d' % (s1[0], s1[2]), 'sim2 final %.5f tr %d' % (s2[0], s2[2]), 'max abs daily diff', np.nanmax(np.abs(d1 - d2)))
    for mk in [False, True]:
        for uw in [False, True]:
            for lev in [1, 10, 20]:
                d3, s3, _ = sim2(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, WLa, WSa, mmr, imr, 2.0, 0.0, 5.0, 168, float(lev), False, FEE_T, FEE_M, RANGE_SLIP, mk, uw, 1)
                m = metrics(d3, days)
                print(pname, 'maker' if mk else 'taker', 'm1' if uw else 'h1', lev, 'cagr %.3f dd %.3f tr %d liq %d fees %.3f legged %d miss %d wbars %d fb %d' % (m['cagr'], s3[1], s3[2], s3[3], s3[7], s3[11], s3[12], s3[13], s3[14]))
