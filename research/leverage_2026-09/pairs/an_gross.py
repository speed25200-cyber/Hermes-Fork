import pandas as pd, numpy as np
d=pd.read_csv('out/grid_gross_1x.csv')
cfg=['method','hedge','Wf','K','Wz','zin','zout','zstop']
i=d[d.period=='IS'].set_index(cfg); o=d[d.period=='OOS'].set_index(cfg)
j=i[['cagr','sharpe','trades']].join(o[['cagr','sharpe','trades']], rsuffix='_oos')
print('gross 1x: IS>0', (j.cagr>0).sum(), 'OOS>0', (j.cagr_oos>0).sum(), 'of', len(j), 'IS med %.3f OOS med %.3f'%(j.cagr.median(), j.cagr_oos.median()))
print(j.groupby(level='method')[['cagr','cagr_oos']].median())
print(j.groupby(level='Wz')[['cagr','cagr_oos']].median())
print(j.groupby(level='zstop')[['cagr','cagr_oos']].median().tail(3))
print(j.sort_values('cagr',ascending=False).head(10).round(3).to_string())
n=pd.read_csv('out/grid_results.csv'); n=n[(n.margin=='cross')&(n.lev==1)]
ni=n[n.period=='IS'].set_index(cfg); no=n[n.period=='OOS'].set_index(cfg)
c=(j.cagr - ni.cagr); print('IS cost drag median %.3f'%c.median(), 'OOS drag %.3f'%(j.cagr_oos-no.cagr).median())
