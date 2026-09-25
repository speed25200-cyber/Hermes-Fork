"""Shared helpers: HTTP via proxy CA bundle, Binance archive (read-only reuse of /home/user/data/raw), S3 listing."""
import io, os, ssl, time, zipfile, urllib.request, urllib.parse, json, re
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')
os.makedirs(D, exist_ok=True)
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
ROOT = 'https://data.binance.vision/'
S3 = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
RAW_CACHE = '/home/user/data/raw/'


def get(url, tries=8, timeout=90):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    raise RuntimeError('failed ' + url)


def s3_list(prefix, delimiter='/'):
    """-> (common prefixes, keys) for a prefix, all pages."""
    pres, keys, marker = [], [], ''
    while True:
        u = f'{S3}?prefix={urllib.parse.quote(prefix)}&delimiter={delimiter}' + (f'&marker={urllib.parse.quote(marker)}' if marker else '')
        x = get(u).decode()
        pres += re.findall(r'<CommonPrefixes><Prefix>(.*?)</Prefix></CommonPrefixes>', x)
        keys += re.findall(r'<Key>(.*?)</Key>', x)
        if '<IsTruncated>true</IsTruncated>' not in x:
            break
        m = re.search(r'<NextMarker>(.*?)</NextMarker>', x)
        marker = m.group(1) if m else (keys[-1] if keys else pres[-1])
    return pres, keys


def get_archive(key):
    p = RAW_CACHE + key
    if os.path.exists(p):
        with open(p, 'rb') as fh:
            return fh.read()
    return get(ROOT + urllib.parse.quote(key))


def parse_klines(b):
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    if not raw:
        return None
    first = raw.split(b'\n', 1)[0]
    hdr = 0 if not first[:1].isdigit() else None
    df = pd.read_csv(io.BytesIO(raw), header=hdr, usecols=[0, 1, 2, 3, 4, 5, 7])
    df.columns = ['t', 'o', 'h', 'l', 'c', 'v', 'qv']
    df = df[pd.to_numeric(df['t'], errors='coerce').notna()]
    t = df['t'].astype(np.int64).values
    t = np.where(t > 10 ** 14, t // 1000, t)
    return pd.DataFrame({'t': t, 'o': df['o'].astype(float).values, 'h': df['h'].astype(float).values,
                         'l': df['l'].astype(float).values, 'c': df['c'].astype(float).values,
                         'v': df['v'].astype(float).values, 'qv': df['qv'].astype(float).values})


def parse_funding(b):
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    if not raw:
        return None
    first = raw.split(b'\n', 1)[0]
    hdr = 0 if not first[:1].isdigit() else None
    df = pd.read_csv(io.BytesIO(raw), header=hdr)
    df = df.iloc[:, [0, 2]]
    df.columns = ['t', 'rate']
    df = df[pd.to_numeric(df['t'], errors='coerce').notna()]
    return pd.DataFrame({'t': df['t'].astype(np.int64).values, 'rate': df['rate'].astype(float).values})


def okx_get(path, tries=6):
    for k in range(tries):
        try:
            b = get('https://www.okx.com' + path, tries=3, timeout=30)
            j = json.loads(b)
            if j.get('code') == '0':
                return j['data']
            if j.get('code') in ('50011',):
                time.sleep(2)
                continue
            return j
        except Exception:
            time.sleep(1 + k)
    raise RuntimeError(path)
