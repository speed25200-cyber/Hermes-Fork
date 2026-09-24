"""Verifier step 1: rebuild walk-forward selections from raw data (no cache), compare with the reported ones,
re-run the IS-selected configs with the hourly (h1) bound and compare with grid_v2.csv.gz."""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *
P, syms, first_bar, U, mmr, imr = prepare()
t = time.time()
sel = build_selections(P, U, first_bar)          # my out/ has no cache -> recomputed
print('selections rebuilt', f'{time.time()-t:.0f}s')
theirs = json.load(open('../pairs/out/selections.json'))
mine = json.load(open('out/selections.json'))
nd = 0
for k in theirs:
    for tt in theirs[k]:
        a = [(x[0], x[1], round(x[2], 8)) for x in theirs[k][tt]]; b = [(x[0], x[1], round(x[2], 8)) for x in mine[k][tt]]
        if a != b: nd += 1
print('selection months differing:', nd, 'of', sum(len(v) for v in theirs.values()))
g = pd.read_csv('../pairs/out/grid_v2.csv.gz')
periods = {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}
picks = [(('coint','lvl',60,3), (336,2.5,0.5,101.5)), (('coint','lvl',60,3), (336,2.5,0.5,4.0)),
         (('coint','ret',60,10), (72,3.0,0.5,4.5)), (('coint','lvl',60,10), (72,3.0,0.5,4.5)),
         (('coint','ret',60,10), (72,2.0,0.5,3.5))]
out = []
for sc, sg in picks:
    x = run_grid2(P, U, sel, None, mmr, imr, [sc], [sg], LEVS, ['cross'], periods, ['taker','maker'], ['h1'], 'rep', log=lambda s: None)
    out.append(x)
x = pd.concat(out, ignore_index=True)
k = ['method','hedge','Wf','K','Wz','zin','zout','zstop','exec','liq','margin','lev','period']
m = x.merge(g[g.liq=='h1'], on=k, suffixes=('_me','_rep'))
m['d_final'] = (m.final_me - m.final_rep).abs()
print(len(x), len(m), 'max |final diff|', m.d_final.max())
print(m[k[3:]+['cagr_me','cagr_rep','trades_me','trades_rep','liqs_me']].round(4).to_string())
x.to_csv('out/v1_repro_h1.csv', index=False)
