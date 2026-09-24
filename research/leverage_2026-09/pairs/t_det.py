import sys
sys.path.insert(0, '.')
from pairs_v2 import *
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
x = run_grid2(P, U, sel, None, mmr, imr, [('coint', 'lvl', 60, 3)], [s for s in SIG if s[0] == 336], LEVS, ['cross', 'sleeve'],
              {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}, ['maker', 'taker'], ['h1'], 'det', log=lambda s: None)
d = pd.read_csv('out/grid_v2.csv.gz')
d = d[(d.method == 'coint') & (d.hedge == 'lvl') & (d.Wf == 60) & (d.K == 3) & (d.Wz == 336) & (d.liq == 'h1')]
k = ['zin', 'zout', 'zstop', 'exec', 'margin', 'lev', 'period']
m = x.merge(d, on=k, suffixes=('_new', '_old'))
print(len(x), len(d), len(m), 'max |cagr diff|', (m.cagr_new - m.cagr_old).abs().max(), 'max |dd diff|', (m.maxdd_intrabar_new - m.maxdd_intrabar_old).abs().max())
