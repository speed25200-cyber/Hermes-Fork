"""Supplement to pairs_v2.py: the 1-minute liquidation bound at L = 1 and 3 (cross margin), which the main v2 run
skipped (it wrongly assumed no liquidation below 5x; the hourly bound does liquidate some 1x configs on 2025-10-10).
Recomputes the 1m bounds (in memory) and stores a compact float16 copy rounded DOWN (conservative) for re-use."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *
import multiprocessing as mp

BFILE = os.path.join(OUT, 'bounds_1m_f16.npz')


def save_bounds(bounds):
    keys = sorted(bounds)
    arr = np.full((len(keys), 2, 744), np.nan, dtype=np.float16)
    for j, k in enumerate(keys):
        for d in range(2):
            v = bounds[k][d]
            v16 = v.astype(np.float16)
            # round toward -inf so the stored bound stays conservative
            low = v16.astype(np.float64) > v
            v16[low] = np.nextafter(v16[low], np.float16(-np.inf))
            arr[j, d, :len(v)] = v16
    meta = np.array([(k[0].value, k[1], k[2], k[3]) for k in keys], dtype=np.float64)
    np.savez_compressed(BFILE, arr=arr, meta=meta)


def load_bounds():
    z = np.load(BFILE)
    out = {}
    for m, a in zip(z['meta'], z['arr']):
        t = pd.Timestamp(int(m[0]))
        n = int(((t + pd.offsets.MonthBegin(1)) - t) / pd.Timedelta(hours=1))
        out[(t, int(m[1]), int(m[2]), round(float(m[3]), 10))] = (a[0, :n].astype(np.float64), a[1, :n].astype(np.float64))
    return out


_G = {}


def _worker(scheme):
    g = _G
    return run_grid2(g['P'], g['U'], g['sel'], g['bounds'], g['mmr'], g['imr'], [scheme], SIG, [1, 3], ['cross'],
                     g['periods'], ['taker', 'maker'], ['m1'], 'v2low', log=lambda s: print(s, flush=True))


if __name__ == '__main__':
    P, syms, first_bar, U, mmr, imr = prepare()
    sel = build_selections(P, U, first_bar)
    if os.path.exists(BFILE):
        bounds = load_bounds()
    else:
        bounds = minute_bound.compute_bounds(sorted(all_pair_months(sel)), list(P['syms']), P['o'].astype(np.float64),
                                             GRID, FORM_DATES, log=lambda s: print(s, flush=True))
        if os.environ.get('SAVE_BOUNDS'):
            save_bounds(bounds)       # off by default: the shared disk is almost full
    print('bounds', len(bounds), flush=True)
    _G.update(P=P, U=U, sel=sel, bounds=bounds, mmr=mmr, imr=imr, periods={'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)})
    with mp.get_context('fork').Pool(4) as pool:
        parts = pool.map(_worker, SCHEMES, chunksize=1)
    df = pd.concat(parts, ignore_index=True)
    df.to_csv(os.path.join(OUT, 'grid_v2_low.csv.gz'), index=False, compression='gzip')
    print(df.shape)
