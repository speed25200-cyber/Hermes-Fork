import sys, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist')
from common import okx_get
ins = okx_get('/api/v5/public/instruments?instType=SWAP')
df = pd.DataFrame(ins)
df['listTime'] = pd.to_datetime(df.listTime.astype('int64'), unit='ms')
cols = ['instId', 'listTime', 'instCategory', 'ruleType', 'ctVal', 'lever', 'maxMktSz', 'maxMktAmt', 'posLmtAmt', 'posLmtPct', 'maxPlatOILmt', 'shortPosRemainingQuota', 'longPosRemainingQuota', 'preMktSwTime', 'openType', 'auctionEndTime']
rec = df.sort_values('listTime').tail(14)
pd.set_option('display.width', 300); pd.set_option('display.max_columns', 30)
print(rec[cols].to_string())
print(df.ruleType.value_counts().to_dict(), df.instCategory.value_counts().to_dict())
print('non-empty posLmtAmt:', (df.posLmtAmt.astype(str) != '').sum(), ' non-empty shortPosRemainingQuota:', (df.shortPosRemainingQuota.astype(str) != '').sum())
x = df[df.shortPosRemainingQuota.astype(str) != '']
print(x[cols].head(20).to_string())
tk = okx_get('/api/v5/market/ticker?instId=KII-USDT-SWAP'); print('KII ticker', tk)
oi = okx_get('/api/v5/public/open-interest?instType=SWAP&instId=KII-USDT-SWAP'); print('KII OI', oi)
