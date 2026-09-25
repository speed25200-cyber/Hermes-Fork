"""Download Binance UM 1h klines for (sym, month) universe pairs not already in ../verify_combo_tsmom/h1 (read-only reuse),
merge -> h1_universe.parquet (sym, t, o, h, l, c) for 2024-12..2026-08."""
import io, ssl, time, zipfile, pickle, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(url, tries=6):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    return None
def parse(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    d = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None)
    if not str(d.iloc[0, 0]).isdigit(): d = d.iloc[1:]
    d = d.iloc[:, :5].astype(float); d.columns = ['t', 'o', 'h', 'l', 'c']; d['t'] = d.t.astype('int64')
    return d
need = pickle.load(open('need_h1.pkl', 'rb'))
def dl(job):
    s, m = job
    b = get(f'https://data.binance.vision/data/futures/um/monthly/klines/{s}/1h/{s}-1h-{m}.zip')
    if b is not None:
        d = parse(b); d['sym'] = s; return job, d
    # month not archived monthly yet (or partial): fall back to daily files
    days = pd.date_range(pd.Period(m).start_time, pd.Period(m).end_time.normalize(), freq='D')
    parts = []
    for dd in days:
        b = get(f'https://data.binance.vision/data/futures/um/daily/klines/{s}/1h/{s}-1h-{dd.date()}.zip')
        if b is not None:
            parts.append(parse(b))
    if parts:
        d = pd.concat(parts); d['sym'] = s; return job, d
    return job, None
with ThreadPoolExecutor(12) as ex:
    res = list(ex.map(dl, need))
miss = [j for j, d in res if d is None]
print('downloaded', sum(d is not None for _, d in res), 'missing', miss)
h = pd.read_parquet('../verify_combo_tsmom/h1/h1_oos.parquet')
out = pd.concat([h] + [d for _, d in res if d is not None], ignore_index=True).drop_duplicates(['sym', 't'])
out.to_parquet('h1_universe.parquet')
print(out.shape, out.sym.nunique())
