"""Verifier step 6: the full 2,016-config maker grid (cross margin, 1m bound, L = 1..20) re-run with a stricter maker
fill rule: a resting limit fills only if the bar trades through it by >= 5 bp (and >= 10 bp), instead of 1 bp.
Everything else identical to pairs_v2.py. Output: out/v6_grid_tick.csv.gz + distribution/IS-selection tables."""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *
from sim3 import sim3
_G = {}

def worker(scheme):
    g = _G; method, hedge, Wf, K = scheme
    WLa, WSa = w_arrays(g['sel'][(method, hedge, Wf)], K, g['bounds'])
    rows = []
    day_all = GRID.normalize()
    for Wz in sorted(set(s[0] for s in SIG)):
        PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(g['P'], g['U'], g['sel'][(method, hedge, Wf)], K, Wz, base_slip)
        for (wz, zin, zout, zstop) in [s for s in SIG if s[0] == Wz]:
            for pname, (p0, p1) in {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}.items():
                i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
                days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D'); DAY = ((day_all - p0).days).values.astype(np.int64)
                for tick in [5e-4, 1e-3]:
                    for lev in LEVS:
                        daily, st, _ = sim3(g['O'], g['H'], g['L'], g['C'], g['MH'], g['ML'], g['FR'], DAY, i0, i1, len(days),
                                            PA, PB, BE, Z, SA, SB, NEWM, WLa, WSa, g['mmr'], g['imr'], zin, zout, zstop, Wz,
                                            float(lev), False, FEE_T, FEE_M, RANGE_SLIP, True, True, 1, tick, 0)
                        m = metrics(daily, days)
                        rows.append(dict(method=method, hedge=hedge, Wf=Wf, K=K, Wz=Wz, zin=zin, zout=zout, zstop=zstop,
                                         tick_bp=tick * 1e4, lev=lev, period=pname, cagr=m['cagr'], sharpe=m['sharpe'],
                                         maxdd=st[1], worst_day=m['worst_day'], trades=int(st[2]), liqs=int(st[3]),
                                         legged=int(st[11]), miss=int(st[12]),
                                         per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()})))
    print('done', scheme, flush=True)
    return pd.DataFrame(rows)

if __name__ == '__main__':
    import multiprocessing as mp
    P, syms, first_bar, U, mmr, imr = prepare()
    sel = build_selections(P, U, first_bar)
    t = time.time()
    bounds = minute_bound.compute_bounds(sorted(all_pair_months(sel)), list(P['syms']), P['o'].astype(np.float64), GRID,
                                         FORM_DATES, log=lambda s: None)
    print('bounds', len(bounds), f'{time.time()-t:.0f}s', flush=True)
    _G.update(P=P, U=U, sel=sel, bounds=bounds, mmr=mmr, imr=imr, O=P['o'].astype(np.float64), H=P['h'].astype(np.float64),
              L=P['l'].astype(np.float64), C=P['c'].astype(np.float64), MH=P['mh'].astype(np.float64),
              ML=P['ml'].astype(np.float64), FR=P['fr'].astype(np.float64))
    with mp.get_context('fork').Pool(3) as pool:
        parts = pool.map(worker, SCHEMES, chunksize=1)
    df = pd.concat(parts, ignore_index=True)
    df.to_csv('out/v6_grid_tick.csv.gz', index=False, compression='gzip')
    print(df.shape, flush=True)
