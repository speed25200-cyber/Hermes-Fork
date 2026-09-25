import sys, json, time, urllib.request
sys.path.insert(0,'.')
from dl import CTX
import pandas as pd
def rest(path):
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request('https://www.okx.com'+path,headers={'User-Agent':'curl/8.0'}),context=CTX,timeout=60) as r:
                d=json.loads(r.read())
            if d.get('code')=='0': return d['data']
            time.sleep(1+k)
        except Exception: time.sleep(1+k)
    return None
out=[]
for inst in sys.argv[1:]:
    after=''; rows=[]
    for _ in range(20):
        d=rest(f'/api/v5/public/funding-rate-history?instId={inst}&limit=100'+(f'&after={after}' if after else ''))
        time.sleep(0.25)
        if not d: break
        rows+=d; after=d[-1]['fundingTime']
        if len(d)<100: break
    df=pd.DataFrame([dict(instId=inst,funding_time=int(x['fundingTime']),rate=float(x['realizedRate'] or x['fundingRate']),fundingRate=float(x['fundingRate']),method=x.get('method')) for x in rows])
    print(inst,len(df),pd.to_datetime(df.funding_time.min(),unit='ms'),pd.to_datetime(df.funding_time.max(),unit='ms'),flush=True)
    out.append(df)
pd.concat(out).drop_duplicates(['instId','funding_time']).sort_values(['instId','funding_time']).to_parquet('fund_rest.parquet')
