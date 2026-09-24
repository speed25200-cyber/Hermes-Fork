import pandas as pd, numpy as np, json, multiprocessing as mp
from load import load
import cb_v as cb
AS = load()
IS = ('2022-01-01', '2024-12-31 23:55'); OOS = ('2025-01-01', '2026-08-31 23:55'); FULL = ('2022-01-01', '2026-08-31 23:55')
g = pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/calbasis/grid_is_lev.csv')
def one(arg):
    rank, p, L = arg
    _, mi, _ = cb.run_assets([AS['BTC'], AS['ETH']], p, L, 'cross', *IS)
    _, mo, _ = cb.run_assets([AS['BTC'], AS['ETH']], p, L, 'cross', *OOS)
    sims, mf, _ = cb.run_assets([AS['BTC'], AS['ETH']], p, L, 'cross', *FULL)
    ecf, elf = cb.equity_series(sims, [.5, .5], *FULL)
    e0 = ecf[:'2024-12-31 23:55'].iloc[-1]; e1 = ecf.iloc[-1]
    seg = (e1 / e0) ** (365.25 / 608) - 1
    return dict(rank=rank, L=L, params=json.dumps(p), is_cagr=round(mi['cagr']*100, 2), oos_cagr=round(mo['cagr']*100, 2),
                oos_dd=round(mo['maxdd']*100, 1), fullrun_oos_seg_cagr=round(seg*100, 2), liq=mo['nliq'] + mf['nliq'])
if __name__ == '__main__':
    args = []
    for L in [10, 20]:
        d = g[(g.fam == 'short') & (g.L == L) & (g.nentry + g.nroll >= 6)].sort_values('cagr', ascending=False).drop_duplicates('cagr').head(10)
        for r, pj in enumerate(d.params):
            args.append((r + 1, json.loads(pj), L))
    B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
    for L in [1, 10, 20]:
        args.append((0, dict(B, min_age_bars=288), L))   # robustness: no entry during a contract's first 24h
    with mp.Pool(4) as pool:
        res = pool.map(one, args)
    df = pd.DataFrame(res); pd.set_option('display.width', 250)
    print(df.to_string())
    for L in [10, 20]:
        d = df[(df.L == L) & (df['rank'] > 0)]
        print(L, 'top-10 IS configs: median OOS', d.oos_cagr.median(), 'median full-run OOS seg', d.fullrun_oos_seg_cagr.median(),
              'share OOS>0', (d.oos_cagr > 0).mean())
    df.to_csv('selection_topIS.csv', index=False)
