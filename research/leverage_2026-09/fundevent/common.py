"""Shared helpers: HTTP with the proxy CA bundle, Binance archive zip parsing."""
import io, os, ssl, time, zipfile, urllib.request, urllib.parse
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')          # symlink to /dev/shm/fundevent (the main disk is full)
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
ROOT = 'https://data.binance.vision/'
RAW_CACHE = '/home/user/data/raw/'      # read-only reuse of identical archive files when present


def get(url, tries=8):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
            with urllib.request.urlopen(req, context=CTX, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    raise RuntimeError('failed ' + url)


def get_archive(key):
    """key like data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2023-01.zip"""
    p = RAW_CACHE + key
    if os.path.exists(p):
        with open(p, 'rb') as fh:
            return fh.read()
    return get(ROOT + urllib.parse.quote(key))


def parse_klines(b):
    """-> DataFrame t(ms int64), o, h, l, c, qv (float64); handles header / no header / microsecond stamps."""
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    if not raw:
        return None
    first = raw.split(b'\n', 1)[0]
    hdr = 0 if not first[:1].isdigit() else None
    df = pd.read_csv(io.BytesIO(raw), header=hdr, usecols=[0, 1, 2, 3, 4, 7])
    df.columns = ['t', 'o', 'h', 'l', 'c', 'qv']
    df = df[pd.to_numeric(df['t'], errors='coerce').notna()]
    t = df['t'].astype(np.int64).values
    t = np.where(t > 10 ** 14, t // 1000, t)       # spot archives since 2025 use microseconds
    out = pd.DataFrame({'t': t, 'o': df['o'].astype(float).values, 'h': df['h'].astype(float).values,
                        'l': df['l'].astype(float).values, 'c': df['c'].astype(float).values,
                        'qv': df['qv'].astype(float).values})
    return out
