import pandas as pd, numpy as np, os, sys
OUT = 'out'
pd.set_option('display.width', 250); pd.set_option('display.max_colwidth', 110)
z = pd.read_csv(os.path.join(OUT, 'grid_zerocost_wick_grid_orb.csv'))
p = z.pivot_table(index=['family', 'cfg_id'], columns='period', values=['cagr', 'sharpe', 'mean_trade', 't_trade', 'trades']).reset_index()
p.columns = ['_'.join([c for c in col if c]) for col in p.columns]
for fam, g in p.groupby('family'):
    print(f'== {fam} ZERO-COST L=1: configs {len(g)}, IS>0 {int((g.cagr_IS>0).sum())}, OOS>0 {int((g.cagr_OOS>0).sum())}, both {int(((g.cagr_IS>0)&(g.cagr_OOS>0)).sum())}, '
          f'median CAGR IS {g.cagr_IS.median():.3f} OOS {g.cagr_OOS.median():.3f}; max IS {g.cagr_IS.max():.3f} OOS {g.cagr_OOS.max():.3f}')
    if fam != 'grid':
        print(f'   per-trade gross mean (bp) median IS {1e4*g.mean_trade_IS.median():.2f} OOS {1e4*g.mean_trade_OOS.median():.2f}; '
              f'configs with gross mean>0 IS {int((g.mean_trade_IS>0).sum())} OOS {int((g.mean_trade_OOS>0).sum())}; t>2 IS {int((g.t_trade_IS>2).sum())} OOS {int((g.t_trade_OOS>2).sum())}')
    cfg = z.drop_duplicates(['family', 'cfg_id']).set_index(['family', 'cfg_id'])['cfg']
    top = g.sort_values('cagr_IS', ascending=False).head(5)
    for _, r in top.iterrows():
        print('   top-IS gross:', cfg[(fam, r.cfg_id)], f'IS {r.cagr_IS:.3f} OOS {r.cagr_OOS:.3f}', '' if fam == 'grid' else f'trade bp IS {1e4*r.mean_trade_IS:.1f} OOS {1e4*r.mean_trade_OOS:.1f} n {int(r.trades_IS)}/{int(r.trades_OOS)}')
