import os, ssl, urllib.request, time, sys
from concurrent.futures import ThreadPoolExecutor
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
BASE = 'https://data.binance.vision/'
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data'
SYMS = ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','BNBUSDT','ADAUSDT','LINKUSDT','AVAXUSDT','LTCUSDT']
months = []
y, m = 2021, 12
while (y, m) <= (2026, 8):
    months.append(f'{y}-{m:02d}')
    m += 1
    if m == 13: y, m = y+1, 1
jobs = []
for s in SYMS:
    for mo in months:
        jobs.append(f'data/futures/um/monthly/klines/{s}/1h/{s}-1h-{mo}.zip')
        jobs.append(f'data/spot/monthly/klines/{s}/1h/{s}-1h-{mo}.zip')
        jobs.append(f'data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{mo}.zip')
        jobs.append(f'data/futures/um/monthly/premiumIndexKlines/{s}/1h/{s}-1h-{mo}.zip')
        jobs.append(f'data/futures/um/monthly/markPriceKlines/{s}/1h/{s}-1h-{mo}.zip')
        jobs.append(f'data/futures/um/monthly/indexPriceKlines/{s}/1h/{s}-1h-{mo}.zip')
def fetch(p):
    dst = os.path.join(OUT, p.replace('/', '__'))
    if os.path.exists(dst) and os.path.getsize(dst) > 0: return 'cached'
    for k in range(4):
        try:
            with urllib.request.urlopen(BASE + p, context=CTX, timeout=60) as r:
                b = r.read()
            with open(dst + '.tmp', 'wb') as f: f.write(b)
            os.rename(dst + '.tmp', dst)
            return 'ok'
        except urllib.error.HTTPError as e:
            if e.code == 404: return '404'
            time.sleep(2 * (k + 1))
        except Exception as e:
            time.sleep(2 * (k + 1))
    return 'fail'
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(fetch, jobs))
from collections import Counter
print(Counter(res))
for p, r in zip(jobs, res):
    if r not in ('ok', 'cached'): print(r, p)
