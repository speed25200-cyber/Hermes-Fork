"""Fetch OKX SWAP instrument meta (tick size) and cross-margin position tiers for the study universe.
GET /api/v5/public/instruments?instType=SWAP ; GET /api/v5/public/position-tiers (tdMode=cross)."""
import json, ssl, urllib.request, time
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(u):
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            err = e; time.sleep(1 + k)
    raise err
COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'LINK', 'AVAX', 'BNB']
ins = get('https://www.okx.com/api/v5/public/instruments?instType=SWAP')['data']
out = {}
for c in COINS:
    iid = f'{c}-USDT-SWAP'
    m = [x for x in ins if x['instId'] == iid]
    if not m: print('missing', iid); continue
    m = m[0]
    t = get(f'https://www.okx.com/api/v5/public/position-tiers?instType=SWAP&tdMode=cross&instFamily={c}-USDT')['data']
    tiers = [dict(tier=int(x['tier']), minSz=float(x['minSz']), maxSz=float(x['maxSz']), mmr=float(x['mmr']), imr=float(x['imr']), maxLever=float(x['maxLever'])) for x in t]
    tiers.sort(key=lambda z: z['tier'])
    out[c] = dict(instId=iid, tickSz=float(m['tickSz']), ctVal=float(m['ctVal']), lever=m['lever'], tiers=tiers[:6])
    print(c, out[c]['tickSz'], out[c]['ctVal'], [(z['tier'], z['maxSz'], z['mmr'], z['maxLever']) for z in tiers[:4]])
json.dump(out, open('okx_meta.json', 'w'), indent=1)
