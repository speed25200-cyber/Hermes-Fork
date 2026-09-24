"""Download Binance USD-M 1m markPrice and indexPrice klines for BTC/ETH 2022-01..2026-08, keep compact float32 arrays (high/low/close), delete zips."""
import os, ssl, urllib.request, io, zipfile, time
import numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify/m1'
os.makedirs(OUT, exist_ok=True)
months = [f'{y}-{m:02d}' for y in range(2022, 2027) for m in range(1, 13) if (y, m) <= (2026, 8)]
jobs = [(s, k, mo) for s in ['BTCUSDT', 'ETHUSDT'] for k in ['markPriceKlines', 'indexPriceKlines'] for mo in months]
def fetch(j):
    s, k, mo = j
    dst = f'{OUT}/{s}_{k}_{mo}.parquet'
    if os.path.exists(dst): return 'cached'
    url = f'https://data.binance.vision/data/futures/um/monthly/{k}/{s}/1m/{s}-1m-{mo}.zip'
    for a in range(4):
        try:
            b = urllib.request.urlopen(url, context=CTX, timeout=90).read(); break
        except urllib.error.HTTPError as e:
            if e.code == 404: return '404 ' + url
            time.sleep(3)
        except Exception:
            time.sleep(3)
    else:
        return 'fail ' + url
    z = zipfile.ZipFile(io.BytesIO(b)); raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None if raw[:1].isdigit() else 0).iloc[:, :5]
    df.columns = ['t', 'o', 'h', 'l', 'c']
    df['t'] = pd.to_datetime(df.t.astype('int64'), unit='ms', utc=True)
    df = df.drop_duplicates('t').set_index('t')[['h', 'l', 'c']].astype('float32')
    df.to_parquet(dst)
    return 'ok'
with ThreadPoolExecutor(6) as ex:
    res = list(ex.map(fetch, jobs))
from collections import Counter
print(Counter(r.split()[0] for r in res))
for r in res:
    if not r.startswith(('ok', 'cached')): print(r)
