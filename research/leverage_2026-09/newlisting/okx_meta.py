"""OKX public REST: current USDT swap instruments (listTime, ctVal, lotSz, minSz, max lever) and cross position tiers.
-> data/okx_instruments.json, data/okx_tiers.json"""
from common import *
inst = okx_get('/api/v5/public/instruments?instType=SWAP')
json.dump(inst, open(os.path.join(D, 'okx_instruments.json'), 'w'))
usdt = [x for x in inst if x['instId'].endswith('-USDT-SWAP')]
print(len(inst), 'swaps;', len(usdt), 'USDT swaps; categories', pd.Series([x.get('instCategory') for x in usdt]).value_counts().to_dict())
out = {}
for x in usdt:
    fam = x['instFamily']
    r = okx_get(f'/api/v5/public/position-tiers?instType=SWAP&tdMode=cross&instFamily={fam}')
    if isinstance(r, list):
        out[x['instId']] = sorted([[float(t['maxSz']), float(t['mmr']), float(t['imr']), float(t['maxLever'])] for t in r])
    time.sleep(0.12)
json.dump(out, open(os.path.join(D, 'okx_tiers.json'), 'w'))
print(len(out), 'tiers')
