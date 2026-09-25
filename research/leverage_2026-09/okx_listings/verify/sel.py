import pandas as pd, numpy as np
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
tr=pd.read_csv(f'{SP}/results/okx_trades.csv')
G0=pd.Timestamp('2021-12-01')
ev=pd.read_parquet(f'{SP}/data/sim/events.parquet')
h1=pd.read_parquet(f'{SP}/data/ev_h1.parquet'); src=h1.groupby('instId').src.first()
tr['src']=tr.sym.map(src)
s=tr.sort_values('ret')
sel=pd.concat([s.head(12),s.tail(8)])
# stop px = 1.5 * first entry of the listing (shared)
first=tr.sort_values(['i','tranche']).groupby('i').entry_px.first()
sel['stop_px']=sel.i.map(first)*1.5
sel['t0']=sel.i.map(ev.t0); sel['t0_exact']=sel.i.map(ev.t0_exact)
sel['entry_ts']=G0+pd.to_timedelta(sel.entry_t,unit='h'); sel['exit_ts']=G0+pd.to_timedelta(sel.exit_t,unit='h')
sel['t0_ts']=pd.to_datetime(sel.t0,unit='ms')
sel['exit_px_implied']=sel.entry_px*(1-sel.ret)
sel.to_csv('sel.csv',index=False)
pd.set_option('display.width',250)
print(sel[['sym','tranche','t0_ts','entry_ts','entry_px','exit_ts','exit_px_implied','stop_px','ret','reason','src']].to_string())
