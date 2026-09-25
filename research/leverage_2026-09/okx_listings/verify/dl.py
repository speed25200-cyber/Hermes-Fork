"""Independent downloader (own code, not the author's helpers) for OKX daily trade archives and Bybit trade archives."""
import os, ssl, time, urllib.request, urllib.error, io, zipfile, gzip, json
import pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
V = os.path.dirname(os.path.abspath(__file__))
def fetch(url, fn, tries=5):
    if os.path.exists(fn):
        return fn if os.path.getsize(fn) > 0 else None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=180) as r:
                b = r.read()
            open(fn, 'wb').write(b); return fn
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                open(fn, 'wb').close(); return None
            time.sleep(2 + 2 * k)
        except Exception as e:
            time.sleep(2 + 2 * k)
    raise RuntimeError(url)
def okx_day(inst, day):
    day = pd.Timestamp(day)
    url = f'https://static.okx.com/cdn/okex/traderecords/trades/daily/{day:%Y%m%d}/{inst}-trades-{day:%Y-%m-%d}.zip'
    return fetch(url, os.path.join(V, 'cache', 'okx', f'{inst}-{day:%Y-%m-%d}.zip'))
def okx_read(fn):
    z = zipfile.ZipFile(fn)
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), encoding='latin-1')
    df.columns = [c.strip().lower() for c in df.columns]
    return df
def bybit_day(sym, day):
    day = pd.Timestamp(day)
    url = f'https://public.bybit.com/trading/{sym}/{sym}{day:%Y-%m-%d}.csv.gz'
    return fetch(url, os.path.join(V, 'cache', 'bybit', f'{sym}{day:%Y-%m-%d}.csv.gz'))
def bybit_read(fn):
    return pd.read_csv(fn, compression='gzip')
