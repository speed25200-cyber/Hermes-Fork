import os, json
import numpy as np, pandas as pd
SRC = '../pairs/out'
CFG = ['method','hedge','Wf','K','Wz','zin','zout','zstop','exec','margin']
v2 = pd.read_csv(f'{SRC}/grid_v2.csv.gz'); low = pd.read_csv(f'{SRC}/grid_v2_low.csv.gz')
print(v2.shape, low.shape)
print(v2.groupby(['liq','margin','exec','period']).size().unstack(-1))
print(low.groupby(['liq','margin','exec','lev','period']).size().unstack(-1))
df = pd.concat([v2, low], ignore_index=True)
# duplicates check
dup = df.duplicated(CFG+['lev','liq','period']).sum(); print('dups', dup, 'nan cagr', df.cagr.isna().sum(), 'nan fees', df.fees.isna().sum())
m1 = df[df.liq=='m1']
i = m1[m1.period=='IS'].set_index(CFG+['lev']); o = m1[m1.period=='OOS'].set_index(CFG+['lev'])
j = i[['cagr','sharpe','maxdd_intrabar','worst_day','trades','liqs','per_year']].join(o[['cagr','sharpe','maxdd_intrabar','worst_day','trades','liqs','per_year']], lsuffix='_is', rsuffix='_oos')
rows=[]
for (ex,mg,lev),g in j.groupby([j.index.get_level_values('exec'), j.index.get_level_values('margin'), j.index.get_level_values('lev')]):
    rows.append(dict(exec=ex,margin=mg,lev=lev,n=len(g),oos_pos=int((g.cagr_oos>0).sum()),both=int(((g.cagr_is>0)&(g.cagr_oos>0)).sum()),
        med_is=round(g.cagr_is.median(),3), med_oos=round(g.cagr_oos.median(),3), q75_oos=round(g.cagr_oos.quantile(.75),3), ruined=int((g.cagr_oos<=-0.999).sum())))
print(pd.DataFrame(rows).to_string(index=False))
pd.set_option('display.width',250)
for lev,g in j.groupby(level='lev'):
    b = g.sort_values(['cagr_is','sharpe_is'],ascending=False).iloc[:3]
    print(lev); print(b[['cagr_is','cagr_oos','sharpe_oos','maxdd_intrabar_oos','worst_day_oos','liqs_oos','trades_is','trades_oos','per_year_is','per_year_oos']].to_string())
