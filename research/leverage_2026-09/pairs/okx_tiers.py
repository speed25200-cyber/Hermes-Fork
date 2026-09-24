"""OKX tier-1 maintenance margin ratio (mmr), initial margin ratio (imr), max leverage for each universe coin.
Source: GET /api/v5/public/position-tiers (instType=SWAP, tdMode=cross), fetched 2026-09-24. Delisted -> NaN."""
import json, os, time, urllib.request, ssl
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__))
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
d = pd.read_parquet(os.path.join(HERE, 'data', 'daily_1d.parquet'), columns=['sym', 'okx']).drop_duplicates('sym')
need = json.load(open(os.path.join(HERE, 'data', 'need_months.json')))
d = d[d.sym.isin(need)]
def one(fam):
    for k in range(4):
        try:
            u = f'https://www.okx.com/api/v5/public/position-tiers?instType=SWAP&tdMode=cross&instFamily={fam}-USDT'
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=30) as r:
                x = json.load(r)
            if x.get('code') != '0' or not x['data']:
                return fam, None
            t = [y for y in x['data'] if y['tier'] == '1'][0]
            return fam, dict(mmr=float(t['mmr']), imr=float(t['imr']), maxlev=float(t['maxLever']), maxsz=float(t['maxSz']))
        except Exception as e:
            time.sleep(1 + k)
    return fam, None
with ThreadPoolExecutor(3) as ex:
    res = dict(ex.map(one, d.okx.unique()))
rows = [dict(sym=s, okx=o, **(res[o] or dict(mmr=float('nan'), imr=float('nan'), maxlev=float('nan'), maxsz=float('nan')))) for s, o in zip(d.sym, d.okx)]
pd.DataFrame(rows).to_csv(os.path.join(HERE, 'data', 'okx_tiers.csv'), index=False)
print(pd.DataFrame(rows).describe(), pd.DataFrame(rows).isna().sum())
