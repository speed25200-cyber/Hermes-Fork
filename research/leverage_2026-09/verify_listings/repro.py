import sys, json, time
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
out = {}
for price in ('hybrid', 'binance'):
    t = time.time()
    Dt = Data(price)
    a = summarize(simulate(Dt, cfg, IS_START, OOS_START))
    b = summarize(simulate(Dt, cfg, OOS_START, OOS_END))
    out[price] = dict(is_=a, oos=b)
    print(price, 'IS', round(a['sharpe'],3), a['trades'], 'OOS', round(b['sharpe'],3), round(b['cagr'],3), round(b['maxdd'],3), b['trades'], b['years'], round(time.time()-t,1), flush=True)
json.dump(out, open('repro.json','w'), indent=1, default=str)
