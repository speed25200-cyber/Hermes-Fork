import pandas as pd, numpy as np
q = pd.read_csv('../pairs/out/grid_15m.csv.gz')
c15 = ['method','hedge','Wf','K','Wz','zin','zout','zstop','exec','lev']
qi = q[q.period=='IS'].set_index(c15); qo = q[q.period=='OOS'].set_index(c15)
cols = ['cagr','sharpe','maxdd_intrabar','worst_day','trades','liqs','per_year']
qj = qi[cols].join(qo[cols], lsuffix='_is', rsuffix='_oos')
pd.set_option('display.width', 250)
for (ex, lev), gg in qj.groupby([qj.index.get_level_values('exec'), qj.index.get_level_values('lev')]):
    print(ex, lev, len(gg), 'oos_pos', int((gg.cagr_oos>0).sum()), 'med_is %.3f med_oos %.3f' % (gg.cagr_is.median(), gg.cagr_oos.median()))
qn = qj[qj.index.get_level_values('exec') != 'gross']
for lev, gg in qn.groupby(level='lev'):
    print(gg.sort_values(['cagr_is','sharpe_is'], ascending=False).iloc[:2][['cagr_is','cagr_oos','sharpe_oos','maxdd_intrabar_oos','liqs_oos','trades_oos','per_year_oos']].to_string())
