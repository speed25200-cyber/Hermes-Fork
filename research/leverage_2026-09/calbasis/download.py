"""Download Binance USDT-M data (5m klines, mark, index, funding) into compact parquet. No zips kept on disk."""
import io, os, ssl, json, time, zipfile, urllib.request, concurrent.futures as cf
import pandas as pd, numpy as np
ctx = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
BASE = 'https://data.binance.vision/'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(OUT, exist_ok=True)
IV = '5m'
def fetch(path, tries=5):
    for i in range(tries):
        try:
            return urllib.request.urlopen(BASE + path, context=ctx, timeout=120).read()
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(2 + 3 * i)
        except Exception:
            time.sleep(2 + 3 * i)
    raise RuntimeError('failed ' + path)
def read_zip(b):
    z = zipfile.ZipFile(io.BytesIO(b)); name = z.namelist()[0]
    df = pd.read_csv(z.open(name), header=None, dtype=str)
    df = df[pd.to_numeric(df[0], errors='coerce').notna()]
    return df
def kl(kind, sym, month):
    b = fetch(f'data/futures/um/monthly/{kind}/{sym}/{IV}/{sym}-{IV}-{month}.zip')
    if b is None: return None
    df = read_zip(b)
    cols = {'klines': [0, 1, 2, 3, 4, 7], 'markPriceKlines': [0, 1, 2, 3, 4], 'indexPriceKlines': [0, 1, 2, 3, 4]}[kind]
    names = {'klines': ['t', 'o', 'h', 'l', 'c', 'qv'], 'markPriceKlines': ['t', 'o', 'h', 'l', 'c'], 'indexPriceKlines': ['t', 'o', 'h', 'l', 'c']}[kind]
    d = df[cols].astype(float); d.columns = names
    d['t'] = d['t'].astype('int64'); d.loc[d.t > 1e14, 't'] //= 1000
    return d
def fund(sym, month):
    b = fetch(f'data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{month}.zip')
    if b is None: return None
    df = read_zip(b)
    d = pd.DataFrame({'t': df[0].astype('int64'), 'rate': df[2].astype(float)})
    return d
def months(a, b):
    return [p.strftime('%Y-%m') for p in pd.period_range(a, b, freq='M')]
def save(name, parts):
    parts = [p for p in parts if p is not None]
    if not parts: print('EMPTY', name); return
    d = pd.concat(parts).drop_duplicates('t').sort_values('t').reset_index(drop=True)
    d.to_parquet(os.path.join(OUT, name + '.parquet'), compression='zstd')
    print(name, len(d), pd.to_datetime(d.t.iloc[0], unit='ms'), pd.to_datetime(d.t.iloc[-1], unit='ms'), flush=True)
def job(spec):
    kind, sym, mlist, name = spec
    fn = os.path.join(OUT, name + '.parquet')
    if os.path.exists(fn): return
    if kind == 'fundingRate': parts = [fund(sym, m) for m in mlist]
    else: parts = [kl(kind, sym, m) for m in mlist]
    save(name, parts)
if __name__ == '__main__':
    listing = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'listing.json')))
    specs = []
    pm = months('2021-11', '2026-08')
    for a in ['BTC', 'ETH']:
        p = a + 'USDT'
        specs += [('klines', p, pm, f'{p}_perp_last'), ('markPriceKlines', p, pm, f'{p}_perp_mark'),
                  ('indexPriceKlines', p, pm, f'{p}_index'), ('fundingRate', p, pm, f'{p}_funding')]
    for s, r in listing.items():
        exp = '20' + s.split('_')[1]; expm = exp[:4] + '-' + exp[4:6]
        ms = [m for m in r['klines'] if m <= expm and m <= '2026-08']
        if not ms or expm < '2022-01': continue
        specs += [('klines', s, ms, f'{s}_last'), ('markPriceKlines', s, ms, f'{s}_mark')]
    print(len(specs), 'series')
    with cf.ThreadPoolExecutor(6) as ex:
        list(ex.map(job, specs))
