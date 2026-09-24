"""Verifier step 8: OOS maker runs with the task's fill rule taken literally: a resting limit fills only if the bar
trades through it by at least one OKX tick (current tickSz / median OOS price), floored at 1 bp.
Applied to (a) the IS-selected configs, (b) every config of the two schemes that contain them (coint/lvl/60 and
coint/ret/60, K = 3/5/10, 72 signal settings = 432 configs) at L = 1..20, 1m bound, cross."""
import os, sys, json, pickle
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *
from sim4 import sim4
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
bounds = {k: (v[0].astype(np.float64), v[1].astype(np.float64)) for k, v in pickle.load(open('out/bounds_2schemes.pkl', 'rb')).items()}
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64); day_all = GRID.normalize(); names = list(P['syms'])
tk = pd.read_csv('out/v7_ticks.csv').set_index('sym').tick_bp
tickv = np.array([max(1.0, tk.get(s, 1.0) if tk.get(s, 1.0) == tk.get(s, 1.0) else 1.0) * 1e-4 for s in names])
tick1 = np.full(len(names), 1e-4)
p0, p1 = OOS0, OOS1; i0 = GRID.get_loc(p0); i1 = NB
days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D'); DAY = ((day_all - p0).days).values.astype(np.int64)
rows = []
for method, hedge, Wf in [('coint', 'lvl', 60), ('coint', 'ret', 60)]:
    for K in [3, 5, 10]:
        WLa, WSa = w_arrays(sel[(method, hedge, Wf)], K, bounds)
        for Wz in [72, 168, 336]:
            PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[(method, hedge, Wf)], K, Wz, base_slip)
            for (wz, zin, zout, zstop) in [s for s in SIG if s[0] == Wz]:
                for lev in LEVS:
                    for tname, tv in (('1bp', tick1), ('1tick', tickv)):
                        daily, st, _ = sim4(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM,
                                            WLa, WSa, mmr, imr, zin, zout, zstop, Wz, float(lev), False, FEE_T, FEE_M,
                                            RANGE_SLIP, True, True, 1, tv, 0)
                        m = metrics(daily, days)
                        rows.append(dict(method=method, hedge=hedge, Wf=Wf, K=K, Wz=Wz, zin=zin, zout=zout, zstop=zstop,
                                         lev=lev, rule=tname, cagr=m['cagr'], maxdd=st[1], liqs=int(st[3]),
                                         per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()})))
R = pd.DataFrame(rows)
R.to_csv('out/v8_tick_percoin.csv.gz', index=False, compression='gzip')
pd.set_option('display.width', 250)
d = R.groupby(['rule', 'lev']).agg(n=('cagr', 'size'), oos_pos=('cagr', lambda x: int((x > 0).sum())), med=('cagr', 'median'),
                                   q90=('cagr', lambda x: x.quantile(0.9)), ruined=('cagr', lambda x: int((x <= -0.999).sum())))
print(d.round(4).to_string())
picks = [('coint', 'lvl', 60, 3, 336, 2.5, 0.5, 101.5, 1), ('coint', 'lvl', 60, 3, 336, 2.5, 0.5, 101.5, 3),
         ('coint', 'lvl', 60, 3, 336, 2.5, 0.5, 4.0, 5), ('coint', 'ret', 60, 10, 72, 3.0, 0.5, 4.5, 10),
         ('coint', 'lvl', 60, 10, 72, 3.0, 0.5, 4.5, 15), ('coint', 'ret', 60, 10, 72, 2.0, 0.5, 3.5, 20)]
for p in picks:
    q = R[(R.method == p[0]) & (R.hedge == p[1]) & (R.Wf == p[2]) & (R.K == p[3]) & (R.Wz == p[4]) & (R.zin == p[5]) &
          (R.zout == p[6]) & (R.zstop == p[7]) & (R.lev == p[8])]
    print(p, q.set_index('rule')[['cagr', 'maxdd', 'liqs', 'per_year']].round(4).to_dict('index'))
