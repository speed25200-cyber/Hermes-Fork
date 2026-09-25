"""Spot-start evidence per base asset from several venues.
bybit_spot_first(base): first trade ts in Bybit public spot archive (BASEUSDT, first monthly file, 10th trade)
gate_start(base): Gate BASE_USDT buy_start (current pairs only)
okx_spot_first(base, t0): first OKX BASE-USDT spot archive day (UTC+8) within [t0-3d, t0+540d] + first trade ts
bn_spot_first(base): Binance spot first 1d kline (first monthly file) over quotes USDT/BUSD/USDC/FDUSD/BTC/BNB/TRY"""
import sys, re, io, zlib, json, zipfile, urllib.parse, functools
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import get, day8, STATIC, read_trades, CTX
import urllib.request
import pandas as pd, numpy as np
HERE = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist/verify/premkt"


def get_range(url, n=40000):
    for k in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0', 'Range': f'bytes=0-{n}'})
            with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
    return None


@functools.lru_cache(None)
def bybit_dirs():
    x = get('https://public.bybit.com/spot/').decode()
    return set(re.findall(r'href="([A-Z0-9]+)"', x))


def bybit_spot_first(base):
    s = base + 'USDT'
    if s not in bybit_dirs():
        return None
    x = get(f'https://public.bybit.com/spot/{s}/').decode()
    fs = sorted(re.findall(rf'href="({s}-\d{{4}}-\d{{2}}(?:-\d{{2}})?\.csv\.gz)"', x))
    if not fs:
        return None
    b = get_range(f'https://public.bybit.com/spot/{s}/{fs[0]}')
    t = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(b).decode(errors='ignore').splitlines()[1:]
    ts = [int(l.split(',')[1]) for l in t[:-1] if l.count(',') >= 4]
    if not ts:
        return None
    ts = sorted(ts)
    return dict(bybit_first=pd.Timestamp(ts[0], unit='ms'), bybit_10th=pd.Timestamp(ts[min(9, len(ts) - 1)], unit='ms'),
                bybit_file=fs[0])


@functools.lru_cache(None)
def gate_pairs():
    g = json.load(open(f'{HERE}/gate_pairs.json'))
    return {p['base']: p for p in g if p['quote'] == 'USDT'}


def gate_start(base):
    p = gate_pairs().get(base)
    if not p:
        return None
    return dict(gate_start=pd.Timestamp(int(p['buy_start']), unit='s'), gate_name=p.get('base_name'), gate_status=p['trade_status'])


def okx_url(base, day):
    return f'{STATIC}/trades/daily/{day:%Y%m%d}/{base}-USDT-trades-{day:%Y-%m-%d}.zip'


OFFS = list(range(-3, 11)) + [14, 21, 30, 45, 60, 90, 120, 180, 270, 365, 540]


def okx_spot_first(base, t0_ms, horizon=pd.Timestamp('2026-09-24')):
    d0 = day8(t0_ms)
    prev = None
    hit = None
    for o in OFFS:
        day = d0 + pd.Timedelta(days=o)
        if day > horizon:
            break
        if get(okx_url(base, day), head=True):
            hit = o
            break
        prev = o
    if hit is None:
        return dict(okx_spot_day=None, okx_spot_first=None)
    if prev is not None and hit - prev > 1:                 # refine daily in (prev, hit)
        for o in range(prev + 1, hit):
            if get(okx_url(base, d0 + pd.Timedelta(days=o)), head=True):
                hit = o
                break
    day = d0 + pd.Timedelta(days=hit)
    first = None
    if hit > -3:
        b = get(okx_url(base, day))
        if b:
            tr = read_trades(b)
            if len(tr):
                first = pd.Timestamp(int(tr.t.sort_values().iloc[min(9, len(tr) - 1)]), unit='ms')
    return dict(okx_spot_day=day, okx_spot_off=hit, okx_spot_first=first)


S3 = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'


def s3_list(prefix):
    pres, keys, marker = [], [], ''
    while True:
        u = f'{S3}?prefix={urllib.parse.quote(prefix)}&delimiter=/' + (f'&marker={urllib.parse.quote(marker)}' if marker else '')
        x = get(u).decode()
        p = re.findall(r'<CommonPrefixes><Prefix>(.*?)</Prefix></CommonPrefixes>', x)
        k = re.findall(r'<Key>(.*?)</Key>', x)
        pres += p; keys += k
        if '<IsTruncated>true</IsTruncated>' not in x:
            return pres, keys
        m = re.search(r'<NextMarker>(.*?)</NextMarker>', x)
        marker = m.group(1) if m else (k[-1] if k else p[-1])


@functools.lru_cache(None)
def bn_spot_syms():
    return set(x.split('/')[-2] for x in s3_list('data/spot/monthly/klines/')[0])


def bn_spot_first(base):
    best = None
    for q in ('USDT', 'BUSD', 'USDC', 'FDUSD', 'BTC', 'BNB', 'TRY'):
        s = base + q
        if s not in bn_spot_syms():
            continue
        _, keys = s3_list(f'data/spot/monthly/klines/{s}/1d/')
        keys = sorted(k for k in keys if k.endswith('.zip'))
        if not keys:
            continue
        b = get(f'{S3}/{keys[0]}')
        z = zipfile.ZipFile(io.BytesIO(b))
        df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None)
        t = int(df.iloc[0, 0])
        t = t // 1000 if t > 1e14 else t
        d = pd.Timestamp(t, unit='ms')
        if best is None or d < best:
            best = d
    return best
