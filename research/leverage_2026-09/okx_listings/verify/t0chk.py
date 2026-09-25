import pandas as pd, numpy as np, json
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
ev=pd.read_parquet(f'{SP}/data/sim/events.parquet')
sw=json.load(open(f'{SP}/data/okx_swap_instruments.json'))
lt={x['instId']:int(x['listTime']) for x in sw}
fu=pd.read_parquet(f'{SP}/data/ev_funding.parquet')
ff=fu.groupby('instId').funding_time.min()
raw=pd.read_parquet(f'{SP}/../xvenue/data/okx_funding_all.parquet')
raw=raw[raw.instId.isin(ev.sym)].groupby('instId').funding_time.min()
ea=pd.read_csv(f'{SP}/data/okx_events_a.csv').set_index('instId')
ev['listTime']=ev.sym.map(lt)
ev['first_fund']=ev.sym.map(ff); ev['first_fund_raw']=ev.sym.map(raw)
ev['okx_first']=ev.sym.map(ea.okx_first)
H=3600000
for c in ['listTime','first_fund','first_fund_raw','okx_first']:
    ev['d_'+c]=(ev.t0_exact-ev[c])/H
ts=lambda x: pd.to_datetime(x,unit='ms').dt.strftime('%Y-%m-%d %H:%M') if hasattr(x,'dt') or True else x
out=ev[['sym','cls','t0_exact','listTime','first_fund','first_fund_raw','okx_first','d_listTime','d_first_fund','d_first_fund_raw','d_okx_first']].copy()
for c in ['t0_exact','listTime','first_fund','first_fund_raw','okx_first']:
    out[c]=pd.to_datetime(out[c],unit='ms').dt.strftime('%y-%m-%d %H:%M')
pd.set_option('display.width',250); pd.set_option('display.max_rows',200)
out.to_csv('t0_check.csv',index=False)
print(out.round(2).to_string())
print('live with listTime', ev.listTime.notna().sum())
print('|t0-listTime|>1h:', (ev.d_listTime.abs()>1).sum(), ev.loc[ev.d_listTime.abs()>1,['sym','d_listTime']].round(1).values.tolist())
print('t0 - first_fund (h) describe', ev.d_first_fund.describe().round(2).to_dict())
print('first funding more than 1h BEFORE t0:', ev.loc[ev.d_first_fund>1,['sym','d_first_fund']].round(1).values.tolist())
print('first funding more than 12h AFTER t0:', ev.loc[ev.d_first_fund<-12,['sym','d_first_fund']].round(1).values.tolist())
