"""Independent re-computation of trade P&L from the raw per-symbol parquet files (not the simulator's panel).

For a config at 1x / taker, take the simulator's trade list, then for a sample of trades recompute
   entry at the open of bar t_entry, exit at the open of bar t_exit (both legs), notional per leg from the equity
   basis, beta and K, slippage (base + 2% of the execution bar range), taker fee, funding events in (entry, exit]
and compare with the simulator's net P&L / equity basis.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *

cfg = dict(method=sys.argv[1] if len(sys.argv) > 1 else 'coint', hedge=sys.argv[2] if len(sys.argv) > 2 else 'lvl',
           Wf=int(sys.argv[3]) if len(sys.argv) > 3 else 60, K=int(sys.argv[4]) if len(sys.argv) > 4 else 3,
           Wz=int(sys.argv[5]) if len(sys.argv) > 5 else 336, zin=float(sys.argv[6]) if len(sys.argv) > 6 else 2.5,
           zout=float(sys.argv[7]) if len(sys.argv) > 7 else 0.5, zstop=float(sys.argv[8]) if len(sys.argv) > 8 else 101.5)
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64)
K = cfg['K']
PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[(cfg['method'], cfg['hedge'], cfg['Wf'])], K, cfg['Wz'], base_slip)
W0 = np.full((K, NB), np.nan)
res = {}
for pname, (p0, p1) in {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}.items():
    i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
    DAY = ((GRID.normalize() - p0).days).values.astype(np.int64)
    daily, st, TR = sim2(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, W0, W0,
                         mmr, imr, cfg['zin'], cfg['zout'], cfg['zstop'], cfg['Wz'], 1.0, False, FEE_T, FEE_M,
                         RANGE_SLIP, False, False, 100000)
    res[pname] = (TR, daily)

raw = {}
def rawsym(s):
    if s not in raw:
        d = pd.read_parquet(os.path.join(D, 'h', s + '.parquet')).set_index('t')
        f = pd.read_parquet(os.path.join(D, 'f', s + '.parquet')) if os.path.exists(os.path.join(D, 'f', s + '.parquet')) else pd.DataFrame(columns=['t', 'rate'])
        raw[s] = (d, f)
    return raw[s]

rng = np.random.default_rng(0)
rows = []
for pname, (TR, daily) in res.items():
    tr = pd.DataFrame(TR, columns=['k', 'a', 'b', 't0', 't1', 'pnl', 'why', 'dir', 'basis'])
    tr = tr[(tr.why != 5)]
    samp = tr.iloc[rng.choice(len(tr), size=min(15, len(tr)), replace=False)]
    for _, r in samp.iterrows():
        a, b = syms[int(r.a)], syms[int(r.b)]
        t0, t1 = GRID[int(r.t0)], GRID[int(r.t1)]
        k = int(r.k)
        be = BE[k, int(r.t0)]
        eq = r.basis
        nA = 2.0 * (eq / K) / (1 + be); nB = be * nA
        (da, fa_), (db, fb_) = rawsym(a), rawsym(b)
        ms = lambda x: x.value // 10**6
        oa0, ha0, la0 = [float(da.loc[ms(t0), c]) for c in ['o', 'h', 'l']]
        ob0, hb0, lb0 = [float(db.loc[ms(t0), c]) for c in ['o', 'h', 'l']]
        oa1, ha1, la1 = [float(da.loc[ms(t1), c]) for c in ['o', 'h', 'l']]
        ob1, hb1, lb1 = [float(db.loc[ms(t1), c]) for c in ['o', 'h', 'l']]
        sa0 = SA[k, int(r.t0)] + RANGE_SLIP * (ha0 - la0) / oa0; sb0 = SB[k, int(r.t0)] + RANGE_SLIP * (hb0 - lb0) / ob0
        sa1 = SA[k, int(r.t1) - 1] + RANGE_SLIP * (ha1 - la1) / oa1; sb1 = SB[k, int(r.t1) - 1] + RANGE_SLIP * (hb1 - lb1) / ob1
        d = int(r.dir)
        qa = d * nA / oa0; qb = -d * nB / ob0
        ea = oa0 * (1 + d * sa0); eb = ob0 * (1 - d * sb0)
        xa = oa1 * (1 - d * sa1); xb = ob1 * (1 + d * sb1)
        pnl = qa * (xa - ea) + qb * (xb - eb)
        fee = FEE_T * (nA + nB) + FEE_T * (abs(qa) * xa + abs(qb) * xb)
        # funding events with timestamp in (t0, t1]  (position held at the event time)
        fund = 0.0
        for (dd, ff, q) in [(da, fa_, qa), (db, fb_, qb)]:
            ev = ff[(ff.t > ms(t0)) & (ff.t <= ms(t1))]
            for te, rate in zip(ev.t.values, ev.rate.values):
                th = int(round(te / 3600000.0)) * 3600000 - 3600000     # bar whose close is the event
                if th in dd.index:
                    fund += q * float(dd.loc[th, 'c']) * rate
        mine = (pnl - fee - fund) / eq
        rows.append(dict(period=pname, a=a, b=b, t0=str(t0), t1=str(t1), dir=d, sim=r.pnl, indep=mine,
                         diff=r.pnl - mine))
out = pd.DataFrame(rows)
print(out.to_string())
print('max |diff| (fraction of equity):', out['diff'].abs().max())
