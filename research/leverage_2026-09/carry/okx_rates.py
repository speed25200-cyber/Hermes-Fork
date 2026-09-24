import ssl, urllib.request, json, time, pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data/okx_usdt_lending_rate_hourly.csv'
rows = []
after = None
stop = 1640995200000 - 30*86400*1000  # before 2022-01-01
while True:
    url = 'https://www.okx.com/api/v5/finance/savings/lending-rate-history?ccy=USDT&limit=100' + (f'&after={after}' if after else '')
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'curl/8.5.0','Accept':'*/*'}), context=CTX, timeout=30) as r:
                d = json.load(r)
            break
        except Exception as e:
            print('err', e, flush=True); time.sleep(2*(k+1))
    data = d.get('data', [])
    if not data: break
    rows += data
    after = data[-1]['ts']
    if int(after) < stop: break
    time.sleep(0.25)
df = pd.DataFrame(rows)
df['ts'] = pd.to_datetime(df['ts'].astype('int64'), unit='ms', utc=True)
df['rate'] = df['rate'].astype(float); df['lendingRate'] = df['lendingRate'].astype(float)
df = df.drop_duplicates('ts').sort_values('ts')
df[['ts','rate','lendingRate']].to_csv(OUT, index=False)
print(len(df), df.ts.min(), df.ts.max())
