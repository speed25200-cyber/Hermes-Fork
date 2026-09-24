"""Download Binance USDT-M funding-rate history for ALL USDT perpetual symbols (live and delisted) from
data.binance.vision (monthly archives), 2021-11 .. 2026-08, into one parquet (RAM disk; the main disk is full).

Output: data/funding_all.parquet with columns sym, t (settlement time, UTC ms floored to the minute),
interval_h (funding interval in hours as reported by the archive, NaN in old files), rate (per interval).
"""
import io, os, re, ssl, sys, time, zipfile, json, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
LIST = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix={p}&delimiter=/'
ROOT = 'https://data.binance.vision/'


def get(url, tries=8):
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


def list_prefix(p):
    keys, prefixes, marker = [], [], ''
    while True:
        url = LIST.format(p=urllib.parse.quote(p)) + (f'&marker={urllib.parse.quote(marker)}' if marker else '')
        x = get(url).decode()
        keys += re.findall(r'<Key>([^<]+)</Key>', x)
        prefixes += re.findall(r'<Prefix>([^<]+)</Prefix>', x)[1:]
        if '<IsTruncated>true</IsTruncated>' in x:
            marker = (keys[-1] if keys else prefixes[-1])
        else:
            return keys, prefixes


def one_symbol(sym):
    keys, _ = list_prefix(f'data/futures/um/monthly/fundingRate/{sym}/')
    out = []
    for k in keys:
        if not k.endswith('.zip'):
            continue
        m = re.search(r'(\d{4}-\d{2})\.zip$', k)
        if not m or not ('2021-11' <= m.group(1) <= '2026-08'):
            continue
        b = get(ROOT + urllib.parse.quote(k))
        if b is None:
            continue
        z = zipfile.ZipFile(io.BytesIO(b))
        raw = z.read(z.namelist()[0]).decode()
        lines = [l for l in raw.strip().splitlines() if l and l[0].isdigit()]
        for l in lines:
            f = l.split(',')
            t = int(f[0])
            if len(f) >= 3:
                ih = float(f[1]) if f[1] else float('nan')
                r = float(f[2])
            else:
                ih, r = float('nan'), float(f[1])
            out.append((sym, t, ih, r))
    return sym, out


def main():
    _, prefixes = list_prefix('data/futures/um/monthly/fundingRate/')
    syms = sorted(p.rstrip('/').split('/')[-1] for p in prefixes)
    usdt = [s for s in syms if s.endswith('USDT')]
    print('all symbols', len(syms), 'USDT perps', len(usdt), flush=True)
    json.dump(usdt, open(os.path.join(D, 'usdt_symbols.json'), 'w'))
    rows = []
    with ThreadPoolExecutor(24) as ex:
        for i, (s, r) in enumerate(ex.map(one_symbol, usdt)):
            rows += r
            if i % 50 == 0:
                print(i, s, len(rows), flush=True)
    df = pd.DataFrame(rows, columns=['sym', 't_raw', 'interval_h', 'rate'])
    df['t'] = (df['t_raw'] // 60000) * 60000
    df = df.sort_values(['sym', 't']).drop_duplicates(['sym', 't'], keep='last')
    df.to_parquet(os.path.join(D, 'funding_all.parquet'))
    print('rows', len(df), 'symbols with data', df.sym.nunique())


if __name__ == '__main__':
    main()
