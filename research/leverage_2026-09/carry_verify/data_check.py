import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry')
import carry_sim as cs, pandas as pd, numpy as np
P = cs.load()
f = P['fund'][['BTCUSDT','ETHUSDT']]
print('funding events per year (nonzero):'); print((f!=0).groupby(f.index.year).sum())
g = f.groupby(f.index.year).sum()
days = f.groupby(f.index.year).size()/24
print('annualised funding:'); print((g.div(days,axis=0)*365).round(4))
b = P['borrow_okx']; print('borrow mean by year:'); print(b.groupby(b.index.year).mean().round(4))
r = pd.read_csv(cs.D+'/okx_usdt_lending_rate_hourly.csv', parse_dates=['ts']).set_index('ts')
print('rate vs lendingRate by year'); print(r.groupby(r.index.year).mean().round(4))
print('rate==lendingRate share', (r.rate==r.lendingRate).groupby(r.index.year).mean())
# hour gaps
d = r.index.to_series().diff().dt.total_seconds()/3600
print('gaps >1h:', (d>1).sum(), d.max())
# nan checks in panel
for k in ['s_c','f_c','m_c','i_c','m_h','i_h']:
    x = pd.read_parquet(cs.D+f'/panel/{k}.parquet')[['BTCUSDT','ETHUSDT']]
    x = x['2022':]
    print(k, x.isna().sum().to_dict())
