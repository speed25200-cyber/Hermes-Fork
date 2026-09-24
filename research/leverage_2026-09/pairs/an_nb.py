import pandas as pd, numpy as np
d=pd.read_csv('out/grid_v2.csv.gz')
x=d[(d.method=='coint')&(d.hedge=='ret')&(d.Wf==60)&(d.Wz==72)&(d.exec=='maker')&(d.liq=='m1')&(d.margin=='cross')]
i=x[x.period=='IS'].set_index(['K','zin','zout','zstop','lev']); o=x[x.period=='OOS'].set_index(['K','zin','zout','zstop','lev'])
j=i[['cagr','maxdd_intrabar','liqs','per_year']].join(o[['cagr','maxdd_intrabar','liqs','per_year']],rsuffix='_oos')
print(j.xs(10,level='lev').round(3).to_string())
