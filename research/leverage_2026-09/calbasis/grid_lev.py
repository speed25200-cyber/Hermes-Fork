"""IS-only: re-run the parameter grid at each leverage (cross margin) and pick the IS-CAGR-maximising config per family."""
import pickle, json, numpy as np, pandas as pd, multiprocessing as mp, cb_backtest as cb
from grid_is import grid
AS = cb.load_all()
IS = ('2022-01-01', '2024-12-31 23:55')
def job(arg):
    fam, p, L = arg
    _, m, _ = cb.run_assets([AS['BTC'], AS['ETH']], p, L, 'cross', *IS)
    return dict(fam=fam, L=L, **{k: (json.dumps(v) if k == 'years' else v) for k, v in m.items()}, params=json.dumps(p))
if __name__ == '__main__':
    g = grid()
    args = [(f, p, L) for L in [3, 5, 10, 15, 20] for f, p in g]
    with mp.Pool(4) as pool:
        res = pool.map(job, args, chunksize=16)
    df = pd.DataFrame(res); df.to_csv('grid_is_lev.csv', index=False)
    for L in [3, 5, 10, 15, 20]:
        for fam in ['short', 'two', 'long']:
            d = df[(df.fam == fam) & (df.L == L) & (df.nentry + df.nroll >= 6)].sort_values('cagr', ascending=False)
            r = d.iloc[0]
            print(L, fam, f"IS cagr={r.cagr*100:.1f}% dd={r.maxdd*100:.1f}% sh={r.sharpe:.2f} liq={r.nliq}", r.params)
