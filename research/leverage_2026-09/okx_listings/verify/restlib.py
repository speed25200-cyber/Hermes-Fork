import sys, json, time, urllib.request; sys.path.insert(0,'.')
from dl import CTX
import pandas as pd, numpy as np
H=3600000
def rest(path):
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request('https://www.okx.com'+path,headers={'User-Agent':'curl/8.0'}),context=CTX,timeout=60) as r:
                d=json.loads(r.read())
            if d.get('code')=='0': return d['data']
            print(path, d.get('code'), d.get('msg')); return None
        except Exception as e: time.sleep(1+k)
    return None
def candles(ep, inst, a, b):
    rows=[]; after=b+H
    for _ in range(10):
        d=rest(f'/api/v5/market/{ep}?instId={inst}&bar=1H&limit=100&after={after}'); time.sleep(0.2)
        if not d: break
        rows+=d; after=int(d[-1][0])
        if after<=a: break
    if not rows: return None
    df=pd.DataFrame([[int(x[0])]+[float(v) for v in x[1:5]] for x in rows],columns=['t','o','h','l','c'])
    return df[(df.t>=a)&(df.t<=b)].drop_duplicates('t').sort_values('t').set_index('t')
