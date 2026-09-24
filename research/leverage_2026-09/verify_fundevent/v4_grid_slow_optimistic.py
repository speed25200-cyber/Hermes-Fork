"""Verifier step 4: the researcher's full slow grid (336 configs x 6 leverages) re-run with the MOST OPTIMISTIC
liquidation model (checked only at the 1h close, no intrabar extremes).  If high leverage still fails here, the
negative verdict does not depend on the conservative simultaneous-extreme liquidation rule.
Optional argv[1] = 'entry1' -> also delay entries by one hour (entry at the next bar open)."""
import json, sys, os, time, itertools
import numpy as np, pandas as pd
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vslow as fs
HERE = os.path.dirname(os.path.abspath(__file__))
G = fs.G
TAG = sys.argv[1] if len(sys.argv) > 1 else 'closeonly'
if __name__ == '__main__':
    G['liq_mode'] = 'close_only'
    if TAG == 'entry1':
        G['entry_delay'] = 1
        G.pop('liq_mode')
    grid = []
    for thr_in, tof, K, hedge, side, uni in itertools.product([0.5, 1.0, 2.0, 4.0], [0.0, 0.5], [1, 3, 10],
                                                             ['btc', 'none', 'spot'], ['both', 'pos', 'neg'], ['all', 'okx']):
        if hedge == 'spot' and side != 'pos':
            continue
        grid.append((thr_in, tof, K, hedge, side, uni))
    jobs = [(c, L) for c in grid for L in fs.LEVS]
    fs.load()
    t = time.time()
    with Pool(3) as p:
        rows = p.map(fs.run_one, jobs, chunksize=4)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(HERE, f'v4_grid_slow_{TAG}.csv.gz'), index=False)
    out = []
    for L in fs.LEVS:
        for uni in ['all', 'okx', 'any']:
            x = df[df.L == L] if uni == 'any' else df[(df.L == L) & (df.universe == uni)]
            out.append({'L': L, 'universe': uni, 'n': len(x), 'is_pos': int((x.cagr_is > 0).sum()), 'oos_pos': int((x.cagr_oos > 0).sum()),
                        'both_pos': int(((x.cagr_is > 0) & (x.cagr_oos > 0)).sum()),
                        'both_pos_min30trOOS': int(((x.cagr_is > 0) & (x.cagr_oos > 0) & (x.trades_oos >= 30) & (x.trades_is >= 30)).sum()),
                        'median_cagr_oos': x.cagr_oos.median(), 'q75_cagr_oos': x.cagr_oos.quantile(0.75), 'max_cagr_oos': x.cagr_oos.max()})
    o = pd.DataFrame(out)
    o.to_csv(os.path.join(HERE, f'v4_grid_slow_{TAG}_dist.csv'), index=False)
    print(o.round(3).to_string(index=False))
    print('elapsed', round(time.time() - t))
