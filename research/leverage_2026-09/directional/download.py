import ssl, urllib.request, os, concurrent.futures as cf, time
ctx = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
BASE = 'https://data.binance.vision/'
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional/data'
months = []
y, m = 2021, 10
while (y, m) <= (2026, 8):
    months.append(f'{y}-{m:02d}')
    m += 1
    if m == 13: y, m = y + 1, 1
jobs = []
for sym in ['BTCUSDT', 'ETHUSDT']:
    for mo in months:
        jobs.append(f'data/futures/um/monthly/klines/{sym}/5m/{sym}-5m-{mo}.zip')
        jobs.append(f'data/futures/um/monthly/markPriceKlines/{sym}/5m/{sym}-5m-{mo}.zip')
        jobs.append(f'data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{mo}.zip')
def get(path):
    dest = os.path.join(OUT, path.replace('/', '_'))
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return path, 'cached'
    for attempt in range(4):
        try:
            with urllib.request.urlopen(BASE + path, context=ctx, timeout=60) as r:
                data = r.read()
            with open(dest, 'wb') as f: f.write(data)
            return path, len(data)
        except Exception as e:
            err = e; time.sleep(2 * (attempt + 1))
    return path, f'ERR {err}'
with cf.ThreadPoolExecutor(8) as ex:
    res = list(ex.map(get, jobs))
bad = [r for r in res if isinstance(r[1], str) and r[1].startswith('ERR')]
print(len(res), 'files;', len(bad), 'errors'); print(bad[:10])
