import time, itertools, numpy as np
import run_grid as R
coins = ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT']
t=time.time(); Ps = {c: R.prep(c) for c in coins}; print('prep', round(time.time()-t))
for ex, lat, mode, hm in R.EXECS:
    t = time.time()
    for cfg in [(1440,15,0.0,480,1,0),(240,30,0.5,120,0,1),(1440,60,0.0,30,1,1)]:
        trs = [R.trades_for(Ps[c], c, cfg, mode, lat, hedge_mid=hm) for c in coins]
        allt = R.merge(trs)
        rows = R.summarize(allt, R.IS_A, R.IS_B, R.IS_YEARS, dict(seg='IS')) + R.summarize(allt, R.OOS_A, R.OOS_B, R.OOS_YEARS, dict(seg='OOS'))
        r = [x for x in rows if x['margin']=='pm' and x['L'] in (1.0, 10.0)]
        print(ex, lat, cfg, 'n', len(allt), ' | '.join(f"{x['seg']} L{x['L']:.0f} cagr {x['cagr']:+.3f} dd {x['maxdd']:+.3f} liq {x['liqs']} tr {x['trades']} edge {x['edge_bp']:.1f}" for x in r))
    print(ex, 'time', round(time.time()-t, 1))
