"""OKX public position tiers (cross, USDT swaps, crypto category) -> tier-1 maintenance margin ratio and max leverage.
Output data/okx_tier1.json {coin: {"mmr": float, "maxLever": float, "tiers": [[maxSz_contracts, mmr, maxLever], ...], "ctVal": float}}
"""
import json, os, ssl, time, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')


def get(url):
    for k in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(1 + k)
    raise RuntimeError(url)


inst = json.load(open(os.path.join(D, 'okx_instruments.json')))['data']
out = {}
for x in inst:
    if not x['instId'].endswith('-USDT-SWAP') or x.get('instCategory') != '1':
        continue
    fam = x['instFamily']
    r = get(f'https://www.okx.com/api/v5/public/position-tiers?instType=SWAP&tdMode=cross&instFamily={fam}')
    tiers = sorted(((float(t['maxSz']), float(t['mmr']), float(t['maxLever'])) for t in r.get('data', [])), key=lambda z: z[0])
    if tiers:
        out[fam.split('-')[0]] = {'mmr': tiers[0][1], 'maxLever': tiers[0][2], 'tiers': tiers, 'ctVal': float(x['ctVal'])}
    time.sleep(0.22)
json.dump(out, open(os.path.join(D, 'okx_tier1.json'), 'w'))
import numpy as np
m = np.array([v['mmr'] for v in out.values()]); L = np.array([v['maxLever'] for v in out.values()])
print(len(out), 'coins; tier-1 mmr quantiles', np.quantile(m, [0, .1, .25, .5, .75, .9, 1]).round(4))
print('tier-1 maxLever quantiles', np.quantile(L, [0, .1, .25, .5, .75, .9, 1]))
for c in ['BTC', 'ETH', 'SOL', 'DOGE', 'PEPE', 'WIF', 'TRB', 'ORDI']:
    if c in out:
        print(c, out[c]['mmr'], out[c]['maxLever'], out[c]['tiers'][:2])
