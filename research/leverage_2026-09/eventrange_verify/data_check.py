"""Verifier: spot-check the cached compact arrays against freshly downloaded Binance daily archives
(klines, markPriceKlines) and the monthly funding archive, in memory only."""
import io, ssl, zipfile, urllib.request
import numpy as np, pandas as pd
from common import load
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(path):
    with urllib.request.urlopen('https://data.binance.vision/' + path, context=CTX, timeout=60) as r:
        b = r.read()
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        raw = z.read(z.namelist()[0])
    hdr = None if raw[:1].isdigit() else 0
    return pd.read_csv(io.BytesIO(raw), header=hdr)
for coin, day in [('AVAX', '2025-10-10'), ('BTC', '2024-08-05'), ('DOGE', '2022-06-13')]:
    sym = coin + 'USDT'
    d = load(coin)
    k = get(f'data/futures/um/daily/klines/{sym}/1m/{sym}-1m-{day}.zip'); k = k.iloc[:, :6]; k.columns = ['t', 'o', 'h', 'l', 'c', 'v']
    m = get(f'data/futures/um/daily/markPriceKlines/{sym}/1m/{sym}-1m-{day}.zip'); m = m.iloc[:, :5]; m.columns = ['t', 'o', 'h', 'l', 'c']
    k = k.astype(float); m = m.astype(float)
    i = np.searchsorted(d['t'], k.t.values.astype(np.int64))
    ok = d['t'][i] == k.t.values
    dk = max(np.abs(d[x][i] - k[x].values).max() for x in 'ohlc')
    j = np.searchsorted(d['t'], m.t.values.astype(np.int64))
    dm = max(np.abs(d['m' + x][j] - m[x].values).max() for x in 'hlc')
    # alignment test: correlation of mark close with last close at lag 0 vs +-1
    lc = d['c'][j]; mc = m.c.values
    err = {lag: np.abs(np.log(mc[5:-5] / d['c'][j[5:-5] + lag])).mean() * 1e4 for lag in (-1, 0, 1)}
    print(coin, day, 'rows', len(k), 'aligned', ok.all(), 'max |kline diff|', dk, 'max |mark diff|', dm, 'mean |log(mark/last)| bp by lag', {a: round(b, 2) for a, b in err.items()})
# funding check: BTC 2025-03 monthly file vs cached fund array
f = get('data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2025-03.zip')
f.columns = ['calc_time', 'interval', 'rate']
d = load('BTC')
ft = (f.calc_time.values // 60000) * 60000
i = np.searchsorted(d['t'], ft)
print('BTC funding 2025-03: n', len(f), 'flags set', int(d['fflag'][i].sum()), 'max |rate diff|', np.abs(d['fund'][i] - f.rate.values).max(),
      'sum of rates in month', f.rate.sum())
