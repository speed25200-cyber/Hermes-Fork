import sys, json, time
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry')
import carry_sim as cs
import pandas as pd
sel = json.load(open('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/results.json'))
bands = {int(k): v for k, v in sel['selected_always'].items()}
print(bands)
rows=[]
for L in [1,3,5,8,10,15,20]:
    for per,(a,b) in {'IS':('2022-01-01','2024-12-31 23:00'),'OOS':('2025-01-01','2026-08-31 23:00')}.items():
        r = cs.sim(['BTCUSDT','ETHUSDT'], L, band=bands[L], start=a, end=b)
        rows.append(dict(L=L, per=per, cagr=round(r['cagr'],4), maxdd=round(r['maxdd'],4), wd=round(r['worst_day'],4), sh=round(r['sharpe'],2), liq=r['liquidations'], tr=r['trades'], imr=r['imr_violations'], fund=round(r['funding'],3), intr=round(r['interest'],3), fees=round(r['fees'],3)))
print(pd.DataFrame(rows).to_string())
