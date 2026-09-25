"""Event (listing-cluster) bootstrap of Sharpe(variant) - Sharpe(binance_only).
Cluster = base asset (an OKX-first token and its later Binance listing are one cluster). Only newtok events (the only
ones the rule can trade) are resampled. Each replicate re-simulates the full 2022-01-01..2026-09-01 portfolio and the
period Sharpes are taken from slices of its daily returns (s0_repro: slices equal per-period runs for 2022-24,
2025-26, 2022-26; 2026 Jan-Aug differs by ~0.07 because of positions open on Jan 1).
design 'all' : resample all clusters (Binance and OKX events)
design 'okx' : Binance events fixed, resample OKX-extra clusters only
-> s2_eventboot.json"""
import json, sys
from multiprocessing import Pool
from statlib import *
from stat_variants import kw, VARIANTS
NB = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
Db, Do = load()
U = merge(Db, Do)
nt = np.where(U.newtok)[0]
src = U.ev.src.values
base = U.ev.base.values
SL = {pn: (a, (pd.Timestamp(b) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')) for pn, a, b in PERIODS}


def run_all(m, variants):
    out = {}
    for v in variants:
        k = kw(m, v)
        keep = k.pop('keep', None)
        if keep is not None:
            idx = np.where(keep)[0]
            mm, k = take(m, idx), {kk: vv[idx] for kk, vv in k.items()}
        else:
            mm = m
        r = sim_live(mm, start='2022-01-01', end='2026-09-01', **k)['ret']
        out[v] = {pn: sharpe(r[a:b]) for pn, (a, b) in SL.items()}
    return out


def clusters(ix):
    d = {}
    for i in ix:
        d.setdefault(base[i], []).append(i)
    return list(d.values())


CL_ALL = clusters(nt)
CL_OKX = clusters(nt[src[nt] == 'okx'])
FIX_BN = nt[src[nt] == 'binance']


def rep(args):
    design, seed = args
    rng = np.random.default_rng(seed)
    if design == 'all':
        g = rng.integers(0, len(CL_ALL), len(CL_ALL))
        idx = np.concatenate([CL_ALL[j] for j in g])
    else:
        g = rng.integers(0, len(CL_OKX), len(CL_OKX))
        idx = np.concatenate([FIX_BN] + [CL_OKX[j] for j in g])
    return run_all(take(U, np.sort(idx)), VARIANTS)


if __name__ == '__main__':
    point = run_all(U, VARIANTS)
    print('clusters all', len(CL_ALL), 'okx', len(CL_OKX), 'binance events fixed', len(FIX_BN))
    print('point (slices):', {v: {p: round(s, 3) for p, s in d.items()} for v, d in point.items()})
    res = dict(point=point)
    for design in ('all', 'okx'):
        with Pool(4) as pool:
            reps = pool.map(rep, [(design, 1000 * (design == 'okx') + s) for s in range(NB)], chunksize=8)
        rd = {}
        for v in VARIANTS[1:]:
            for pn in SL:
                d = np.array([r[v][pn] - r['binance_only'][pn] for r in reps])
                s = np.array([r[v][pn] for r in reps])
                d0 = point[v][pn] - point['binance_only'][pn]
                c = d - d.mean()
                rd[f'{v} | {pn}'] = dict(d_point=round(d0, 3), d_boot_mean=round(float(d.mean()), 3), se=round(float(d.std()), 3),
                                         ci90=[round(float(np.percentile(d, 5)), 2), round(float(np.percentile(d, 95)), 2)],
                                         ci95=[round(float(np.percentile(d, 2.5)), 2), round(float(np.percentile(d, 97.5)), 2)],
                                         share_below_0=round(float((d < 0).mean()), 3),
                                         p_two_sided_centred=round(float((np.abs(c) >= abs(d0)).mean()), 4),
                                         sr_ci90=[round(float(np.percentile(s, 5)), 2), round(float(np.percentile(s, 95)), 2)])
                print(design, v, pn, rd[f'{v} | {pn}'], flush=True)
        bn = np.array([[r['binance_only'][pn] for pn in SL] for r in reps])
        rd['binance_only_sr_ci90'] = {pn: [round(float(np.percentile(bn[:, j], 5)), 2), round(float(np.percentile(bn[:, j], 95)), 2)]
                                      for j, pn in enumerate(SL)}
        res[design] = rd
        json.dump(res, open(f'{OUT}/s2_eventboot.json', 'w'), indent=1)
