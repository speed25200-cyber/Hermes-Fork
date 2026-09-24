"""Step 1: point-in-time liquidity ranking data.

For every Binance USDT-M perpetual that also has (or had) an OKX USDT-SWAP (OKX funding archive 2022-2025 as the
listing proxy), download monthly 1d klines 2021-07..2026-08 (in memory, no zip on disk) and save one compact parquet
with daily quote volume and close. Used only to rank symbols by trailing 30-day quote volume at each formation date.
"""
import io, os, ssl, json, time, zipfile, urllib.request, urllib.error, re
from concurrent.futures import ThreadPoolExecutor
import pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCR = os.path.dirname(HERE)
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
MONTHS = [f'{y}-{m:02d}' for y in range(2021, 2027) for m in range(1, 13) if (2021, 7) <= (y, m) <= (2026, 8)]


def get(url, tries=6):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
            with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    raise RuntimeError('failed ' + url)


def okx_bases():
    d = pd.read_parquet(os.path.join(SCR, 'xvenue/data/okx_funding_all.parquet'), columns=['instId'])
    return set(i.replace('-USDT-SWAP', '') for i in d.instId.unique() if i.endswith('-USDT-SWAP'))


def okx_base(sym, ok):
    b = sym[:-4]
    for p in ['1000000', '1000', '1M']:
        if b.startswith(p) and b[len(p):] in ok:
            return b[len(p):]
    return b


def list_months(sym):
    url = f'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/monthly/klines/{sym}/1d/&delimiter=/'
    x = get(url).decode()
    return sorted(set(re.findall(rf'{sym}-1d-(\d{{4}}-\d{{2}})\.zip<', x)))


def one(sym, month):
    b = get(f'https://data.binance.vision/data/futures/um/monthly/klines/{sym}/1d/{sym}-1d-{month}.zip')
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    has_header = not raw[:1].isdigit()
    df = pd.read_csv(io.BytesIO(raw), header=0 if has_header else None).iloc[:, [0, 4, 7]]
    df.columns = ['t', 'close', 'qv']
    df = df[pd.to_numeric(df.t, errors='coerce').notna()].astype(float)
    df['sym'] = sym
    return df


if __name__ == '__main__':
    syms = [s for s in json.load(open(os.path.join(SCR, 'xvenue/binance_um_syms.json'))) if s.endswith('USDT')]
    ok = okx_bases()
    syms = [s for s in syms if okx_base(s, ok) in ok]
    print(len(syms), 'symbols with an OKX USDT swap')
    with ThreadPoolExecutor(16) as ex:
        avail = dict(zip(syms, ex.map(list_months, syms)))
    jobs = [(s, m) for s in syms for m in avail[s] if m in MONTHS]
    print(len(jobs), 'files')
    with ThreadPoolExecutor(24) as ex:
        res = list(ex.map(lambda a: one(*a), jobs))
    df = pd.concat([r for r in res if r is not None], ignore_index=True)
    df['t'] = pd.to_datetime(df.t.astype('int64'), unit='ms')
    df['okx'] = df.sym.map(lambda s: okx_base(s, ok))
    df.to_parquet(os.path.join(HERE, 'data', 'daily_1d.parquet'), index=False)
    json.dump({s: avail[s] for s in syms}, open(os.path.join(HERE, 'data', 'avail_months.json'), 'w'))
    print(df.shape, df.t.min(), df.t.max())
