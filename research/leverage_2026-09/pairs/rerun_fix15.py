"""Same NaN-bug fix for the 15m grid: re-run the affected (scheme, Wz, period) maker blocks and splice them in."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import pairs_bt as pb
import pairs_15m as p15
from pairs_v2 import sim2, FEE_M

path = os.path.join(pb.OUT, 'grid_15m.csv.gz')
d = pd.read_csv(path)
bad = d[d.fees.isna()][['method', 'hedge', 'Wf', 'K', 'Wz', 'period']].drop_duplicates()
P, syms, first_bar, U, mmr, imr = pb.prepare()
sel = pb.build_selections(P, U, first_bar)
keys = {(r.method, r.hedge, int(r.Wf)) for r in bad.itertuples()}
A = p15.load15(list(P['syms']), {k: sel[k] for k in keys})
FR = p15.fund15(P)
O = A['o'].astype(np.float64); H = A['h'].astype(np.float64); L = A['l'].astype(np.float64); C = A['c'].astype(np.float64)
del A
logc = np.log(C)
names = list(P['syms'])
day_all = p15.GRID.normalize()
periods = {'IS': (pb.IS0, pb.IS1), 'OOS': (pb.OOS0, pb.OOS1)}
new = []
for r in bad.itertuples():
    K, Wz = int(r.K), int(r.Wz)
    PA, PB, BE, Z, SA, SB, NEWM = p15.slot15(logc, U, names, sel[(r.method, r.hedge, int(r.Wf))], K, Wz)
    W0 = np.full((K, p15.NB), np.nan)
    p0, p1 = periods[r.period]
    i0 = p15.GRID.get_loc(p0); i1 = p15.GRID.get_loc(p1) if p1 < pb.G1 else p15.NB
    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
    DAY = ((day_all - p0).days).values.astype(np.int64)
    for (wz, zin, zout, zstop) in [s for s in p15.SIG15 if s[0] == Wz]:
        for lev in pb.LEVS:
            daily, st, _ = sim2(O, H, L, C, H, L, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, W0, W0, mmr,
                                imr, zin, zout, zstop, Wz, float(lev), False, pb.FEE_T, FEE_M, pb.RANGE_SLIP, True, False, 1)
            assert st[15] == 0
            m = pb.metrics(daily, days)
            new.append(dict(method=r.method, hedge=r.hedge, Wf=int(r.Wf), K=K, Wz=Wz, zin=zin, zout=zout, zstop=zstop,
                            exec='maker', lev=lev, period=r.period, cagr=m['cagr'], final=m['final'],
                            sharpe=m['sharpe'], worst_day=m['worst_day'], maxdd_intrabar=st[1], trades=int(st[2]),
                            liqs=int(st[3]), stops=int(st[4]), fees=st[7], funding=st[8], exposure=st[9],
                            legged=int(st[11]), maker_miss=int(st[12]),
                            per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()})))
    msk = ((d.method == r.method) & (d.hedge == r.hedge) & (d.Wf == r.Wf) & (d.K == r.K) & (d.Wz == r.Wz) &
           (d.period == r.period) & (d.exec == 'maker'))
    print('15m', r.method, r.hedge, r.Wf, K, Wz, r.period, 'replaced', int(msk.sum()), flush=True)
    d = d[~msk]
new = pd.DataFrame(new)
d = pd.concat([d, new], ignore_index=True)
assert d.fees.notna().all()
d.to_csv(path, index=False, compression='gzip')
print('rows', len(d))
