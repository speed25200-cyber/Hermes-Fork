import ssl, urllib.request, json, time, pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
H = {'User-Agent':'curl/8.5.0','Accept':'*/*'}
out = []
for c in ['BTC','ETH','SOL','XRP','DOGE','BNB','ADA','LINK','AVAX','LTC']:
    after = None
    for page in range(20):
        url = f'https://www.okx.com/api/v5/public/funding-rate-history?instId={c}-USDT-SWAP&limit=100' + (f'&after={after}' if after else '')
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=H), context=CTX, timeout=30))
        data = d.get('data', [])
        if not data: break
        for x in data: out.append((c, int(x['fundingTime']), float(x['realizedRate'] or x['fundingRate'])))
        after = data[-1]['fundingTime']; time.sleep(0.3)
df = pd.DataFrame(out, columns=['coin','ts','rate']); df['ts'] = pd.to_datetime(df.ts, unit='ms', utc=True)
df.to_csv('data/okx_funding_recent.csv', index=False)
print(df.groupby('coin').ts.agg(['min','max','count']))
