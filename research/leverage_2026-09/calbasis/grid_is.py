"""In-sample (2022-01-01..2024-12-31) parameter grid. Objective: IS Sharpe of the 50/50 BTC+ETH portfolio at L=1, cross margin.
Nothing from 2025+ is touched here."""
import pickle, itertools, json, numpy as np, pandas as pd, multiprocessing as mp, cb_backtest as cb
AS = cb.load_all()
IS = ('2022-01-01', '2024-12-31 23:55')
def grid():
    out = []
    for tau in [14, 30]:
        for h_in in [0.06, 0.08, 0.10, 0.12, 0.15, 0.20]:
            for h_out in [None, 0.02, 0.04, 0.06, 0.08]:
                if h_out is not None and h_out >= h_in: continue
                out.append(dict(signal='raw', h_in=h_in, h_out=h_out, tau_min=tau))
        for h_in in [-0.02, 0.0, 0.02, 0.04, 0.06, 0.08]:
            for h_out in [None, -0.04, -0.02, 0.0, 0.02, 0.04]:
                if h_out is not None and h_out >= h_in: continue
                out.append(dict(signal='net', h_in=h_in, h_out=h_out, tau_min=tau))
    two = []
    for p in out:
        if p['signal'] == 'raw':
            revs = [(r, o) for r in [0.02, 0.04, 0.06] for o in [None, 0.06, 0.08, 0.10] if o is None or o > r]
        else:
            revs = [(r, o) for r in [-0.02, -0.04, -0.06] for o in [None, -0.02, 0.0, 0.02] if o is None or o > r]
        for r, o in revs:
            if r >= p['h_in']: continue
            two.append(dict(p, h_in_rev=r, h_out_rev=o))
    rev_only = []
    for tau in [14, 30]:
        for r, o in [(r, o) for r in [0.02, 0.04, 0.06, 0.08] for o in [None, 0.06, 0.08, 0.10, 0.12] if o is None or o > r]:
            rev_only.append(dict(signal='raw', h_in=None, h_out=None, h_in_rev=r, h_out_rev=o, tau_min=tau))
        for r, o in [(r, o) for r in [0.0, -0.02, -0.04, -0.06] for o in [None, -0.02, 0.0, 0.02, 0.04] if o is None or o > r]:
            rev_only.append(dict(signal='net', h_in=None, h_out=None, h_in_rev=r, h_out_rev=o, tau_min=tau))
    return [('short', p) for p in out] + [('two', p) for p in two] + [('long', p) for p in rev_only]
def job(arg):
    fam, p = arg
    _, m, _ = cb.run_assets([AS['BTC'], AS['ETH']], p, 1, 'cross', *IS)
    return dict(fam=fam, **{k: (json.dumps(v) if k == 'years' else v) for k, v in m.items()}, params=json.dumps(p))
if __name__ == '__main__':
    g = grid(); print(len(g), 'configs', flush=True)
    with mp.Pool(4) as pool:
        res = pool.map(job, g, chunksize=8)
    df = pd.DataFrame(res)
    df.to_csv('grid_is.csv', index=False)
    for fam in ['short', 'two', 'long']:
        d = df[(df.fam == fam) & (df.nentry + df.nroll >= 6)].sort_values('sharpe', ascending=False)
        print('==', fam, len(d)); print(d.head(12)[['sharpe', 'cagr', 'maxdd', 'nentry', 'nroll', 'params']].to_string())
