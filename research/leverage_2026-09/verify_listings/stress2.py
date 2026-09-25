"""Fair latency stress and stop-slippage stress on hybrid2 prices (OKX REST + OKX trade-archive rebuild)."""
import json
import sim_v as S
from sim_v import *
Dt = Data('hybrid2')
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
cfgL = dict(cfg, d0=73, d1=169)   # entry and exit one bar later; exchange-side stop still fills intrabar
out = {}
def run(c, **kw):
    s = summarize(simulate(Dt, c, OOS_START, OOS_END, **kw)); return dict(sharpe=round(s['sharpe'], 3), y2025=round(s['years'][2025], 3), y2026=round(s['years'][2026], 3), maxdd=round(s['maxdd'], 3))
out['base'] = run(cfg)
out['fair_lat1_cost15'] = run(cfgL, cost_mult=1.5)
out['fair_lat1'] = run(cfgL)
out['researcher_lat1_cost15'] = run(cfg, lat=1, cost_mult=1.5)
for sl in (0.02, 0.05, 0.10):
    S.STOP_SLIP = sl
    out[f'stopslip{int(sl*100)}'] = run(cfg)
    out[f'stopslip{int(sl*100)}_fairlat1_cost15'] = run(cfgL, cost_mult=1.5)
    out[f'stopslip{int(sl*100)}_L3total'] = run(cfg, L=1.5)
    out[f'stopslip{int(sl*100)}_L2total'] = run(cfg, L=1.0)
S.STOP_SLIP = 0.0
# bar item 1 on neighbours: stop variants of the selected window under fair latency
for st in (None, 0.25, 1.0):
    out[f'stop{st}_base'] = run(dict(cfg, stop=st))
json.dump(out, open('stress2_hybrid2.json', 'w'), indent=1)
for k, v in out.items(): print(k, v)
