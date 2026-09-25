"""Shared helpers for the OKX-listing study (proxy CA bundle, OKX REST/static archives, Bybit public archive)."""
import io, os, ssl, time, json, zipfile, urllib.request, urllib.error, threading
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')
os.makedirs(D, exist_ok=True)
NL = os.path.join(os.path.dirname(BASE), 'newlisting', 'data')   # Binance-listing study data (read-only)
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
STATIC = 'https://static.okx.com/cdn/okex/traderecords'


def get(url, tries=6, timeout=90, head=False):
    """Body bytes (or True for HEAD), None on 404."""
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}, method='HEAD' if head else 'GET')
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
                return True if head else r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    raise RuntimeError('failed ' + url)


_lock = threading.Lock()
_last = [0.0]


def okx_get(path, rate=8.0):
    """OKX public REST (data field), throttled to `rate` requests/s across threads."""
    with _lock:
        wait = _last[0] + 1.0 / rate - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    for k in range(6):
        b = get('https://www.okx.com' + path)
        if b is None:
            return None
        d = json.loads(b)
        if d.get('code') == '0':
            return d['data']
        if d.get('code') == '50011':          # rate limit
            time.sleep(1 + k)
            continue
        return None
    return None


def day8(ts_ms):
    """OKX archive day (UTC+8) of a UTC millisecond timestamp."""
    return (pd.Timestamp(int(ts_ms), unit='ms') + pd.Timedelta(hours=8)).normalize()


def trades_url(inst, day):
    return f'{STATIC}/trades/daily/{day:%Y%m%d}/{inst}-trades-{day:%Y-%m-%d}.zip'


def read_trades(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), encoding='latin-1')
    df.columns = [c.strip().lower() for c in df.columns]
    t = [c for c in df.columns if 'time' in c][0]
    return pd.DataFrame({'t': pd.to_numeric(df[t], errors='coerce'), 'px': pd.to_numeric(df['price'], errors='coerce'),
                         'sz': pd.to_numeric(df['size'], errors='coerce')}).dropna()
