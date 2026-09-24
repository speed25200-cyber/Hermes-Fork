import pandas as pd, numpy as np
CFG = ['method','hedge','Wf','K','Wz','zin','zout','zstop','exec','margin']
v2 = pd.read_csv('../pairs/out/grid_v2.csv.gz')
h1 = v2[(v2.liq=='h1')&(v2.margin=='cross')]
for (ex, lev), g in h1[h1.period=='OOS'].groupby(['exec','lev']):
    print('h1', ex, lev, 'oos_pos', int((g.cagr>0).sum()), 'liq', int((g.liqs>0).sum()))
gr = pd.read_csv('../pairs/out/grid_gross_1x.csv.gz')
for p, g in gr.groupby('period'):
    print('gross', p, len(g), 'pos', int((g.cagr>0).sum()), 'median %.4f' % g.cagr.median())
# IS vs OOS correlation at 1x taker (m1 low grid)
low = pd.read_csv('../pairs/out/grid_v2_low.csv.gz')
for ex in ['taker','maker']:
    l = low[(low.lev==1)&(low.exec==ex)]
    j = l[l.period=='IS'].set_index(CFG).cagr.to_frame('is').join(l[l.period=='OOS'].set_index(CFG).cagr.to_frame('oos'))
    print(ex, 'corr IS/OOS cagr 1x', round(j.corr().iloc[0,1],3), 'spearman', round(j.corr('spearman').iloc[0,1],3))
    # top-decile IS -> OOS
    top = j[j['is']>=j['is'].quantile(0.9)]
    print(ex, 'top-decile IS: OOS median %.4f, frac>0 %.3f' % (top.oos.median(), (top.oos>0).mean()))
# fees: implied cost drag at 1x: taker-vs-gross
g1 = gr.set_index(['method','hedge','Wf','K','Wz','zin','zout','zstop','period']).cagr
for ex in ['taker','maker']:
    l = low[(low.lev==1)&(low.exec==ex)].set_index(['method','hedge','Wf','K','Wz','zin','zout','zstop','period']).cagr
    d = (g1 - l).groupby(level='period').median()
    print(ex, 'median CAGR drag vs gross', d.round(4).to_dict())
