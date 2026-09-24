"""Download real historical data for the Binance-vs-OKX cross-venue arbitrage study.

Sources
- Binance USDT-M archive (data.binance.vision): 1h klines, 1h mark-price klines, funding rates (monthly zips).
- OKX static historical files (static.okx.com/cdn/okex/traderecords/swaprate/...): realized funding for ALL swaps, daily files.
- OKX public REST: /api/v5/market/history-candles (1H last-price) and
  /api/v5/market/history-mark-price-candles (1H mark price), paginated with `after`.
Everything is cached under ./data.
"""
import io, os, sys, json, ssl, time, zipfile, threading, urllib.request, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')

UNIVERSE = [  # coin, binance symbol, okx instId, okx->binance price multiplier
    ('BTC', 'BTCUSDT'), ('ETH', 'ETHUSDT'), ('SOL', 'SOLUSDT'), ('XRP', 'XRPUSDT'), ('DOGE', 'DOGEUSDT'),
    ('ADA', 'ADAUSDT'), ('LINK', 'LINKUSDT'), ('AVAX', 'AVAXUSDT'), ('LTC', 'LTCUSDT'), ('DOT', 'DOTUSDT'),
    ('BCH', 'BCHUSDT'), ('TRX', 'TRXUSDT'), ('ATOM', 'ATOMUSDT'), ('ETC', 'ETCUSDT'), ('FIL', 'FILUSDT'),
    ('NEAR', 'NEARUSDT'), ('UNI', 'UNIUSDT'), ('AAVE', 'AAVEUSDT'), ('CRV', 'CRVUSDT'), ('SAND', 'SANDUSDT'),
    ('AXS', 'AXSUSDT'), ('GALA', 'GALAUSDT'), ('XLM', 'XLMUSDT'), ('APT', 'APTUSDT'), ('ARB', 'ARBUSDT'),
    ('OP', 'OPUSDT'), ('SUI', 'SUIUSDT'), ('INJ', 'INJUSDT'), ('SHIB', '1000SHIBUSDT'), ('PEPE', '1000PEPEUSDT'),
    ('WIF', 'WIFUSDT'), ('LDO', 'LDOUSDT'), ('TIA', 'TIAUSDT'), ('BNB', 'BNBUSDT'), ('APE', 'APEUSDT'),
    ('DYDX', 'DYDXUSDT'), ('ORDI', 'ORDIUSDT'), ('WLD', 'WLDUSDT'),
]
MONTHS = [f'{y}-{m:02d}' for y in range(2022, 2027) for m in range(1, 13) if (y, m) <= (2026, 8)]


def get(url, tries=6, binary=True):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
            with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                b = r.read()
            return b if binary else b.decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(2 + 2 * k)
                continue
            time.sleep(1 + k)
        except Exception:
            time.sleep(1 + k)
    raise RuntimeError('failed ' + url)


def read_zip_csv(b, header_guess=True):
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    first = raw.split(b'\n', 1)[0]
    has_header = not first[:1].isdigit()
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)


# ---------------- Binance ----------------
KCOLS = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'count',
         'taker_buy_volume', 'taker_buy_quote_volume', 'ignore']


def binance_one(kind, sym, month):
    root = 'https://data.binance.vision/data/futures/um/monthly'
    if kind == 'klines':
        url = f'{root}/klines/{sym}/1h/{sym}-1h-{month}.zip'
    elif kind == 'mark':
        url = f'{root}/markPriceKlines/{sym}/1h/{sym}-1h-{month}.zip'
    else:
        url = f'{root}/fundingRate/{sym}/{sym}-fundingRate-{month}.zip'
    b = get(url)
    if b is None:
        return None
    df = read_zip_csv(b)
    if kind in ('klines', 'mark'):
        df = df.iloc[:, :6]
        df.columns = KCOLS[:6]
        df = df[pd.to_numeric(df['open_time'], errors='coerce').notna()].astype(float)
    else:
        df.columns = ['calc_time', 'funding_interval_hours', 'last_funding_rate'][:df.shape[1]]
    return df


def fetch_binance(coin, sym):
    out = os.path.join(D, 'binance')
    os.makedirs(out, exist_ok=True)
    for kind in ('klines', 'mark', 'funding'):
        fn = os.path.join(out, f'{coin}_{kind}.parquet')
        if os.path.exists(fn):
            continue
        parts = [binance_one(kind, sym, m) for m in MONTHS]
        parts = [p for p in parts if p is not None and len(p)]
        if not parts:
            print('no binance', kind, sym, flush=True)
            continue
        df = pd.concat(parts, ignore_index=True)
        key = 'open_time' if kind != 'funding' else 'calc_time'
        df = df.drop_duplicates(key).sort_values(key)
        df.to_parquet(fn)
        print('binance', coin, kind, len(df), flush=True)


# ---------------- OKX funding files ----------------
def okx_funding_day(day):
    ym = day.strftime('%Y%m')
    fn = f'allswaprate-swaprate-{day:%Y-%m-%d}.zip'
    url = f'https://static.okx.com/cdn/okex/traderecords/swaprate/monthly/{ym}/{fn}'
    b = get(url)
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=0, encoding='latin-1')
    df.columns = ['instId', 'ctype', 'funding_rate', 'real_funding_rate', 'funding_time']
    df = df[df.instId.str.endswith('-USDT-SWAP')]
    return df


def fetch_okx_funding():
    fn = os.path.join(D, 'okx_funding_all.parquet')
    if os.path.exists(fn):
        return
    days = pd.date_range('2022-01-01', '2026-08-31', freq='D')
    with ThreadPoolExecutor(12) as ex:
        parts = list(ex.map(okx_funding_day, days))
    miss = [str(d.date()) for d, p in zip(days, parts) if p is None]
    print('okx funding missing days:', len(miss), miss[:20], flush=True)
    df = pd.concat([p for p in parts if p is not None], ignore_index=True)
    df['funding_time'] = df['funding_time'].astype('int64')
    df = df.drop_duplicates(['instId', 'funding_time']).sort_values(['instId', 'funding_time'])
    df.to_parquet(fn)
    print('okx funding rows', len(df), flush=True)


# ---------------- OKX candles via REST ----------------
class Throttle:
    def __init__(self, rate):
        self.dt = 1.0 / rate
        self.lock = threading.Lock()
        self.t = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            if now < self.t:
                time.sleep(self.t - now)
            self.t = max(now, self.t) + self.dt


TH = {'candles': Throttle(8.0), 'mark': Throttle(4.0)}
START_MS = int(dt.datetime(2021, 12, 31, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_MS = int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def fetch_okx_candles(coin, kind):
    out = os.path.join(D, 'okx')
    os.makedirs(out, exist_ok=True)
    fn = os.path.join(out, f'{coin}_{kind}.parquet')
    if os.path.exists(fn):
        return
    inst = f'{coin}-USDT-SWAP'
    ep = 'history-candles' if kind == 'candles' else 'history-mark-price-candles'
    after = END_MS
    rows = []
    empty = 0
    while after > START_MS:
        TH[kind].wait()
        url = f'https://www.okx.com/api/v5/market/{ep}?instId={inst}&bar=1H&limit=100&after={after}'
        b = get(url)
        j = json.loads(b)
        if j.get('code') != '0':
            if j.get('code') == '50011':  # rate limit
                time.sleep(2)
                continue
            print('okx err', inst, kind, j, flush=True)
            break
        d = j['data']
        if not d:
            empty += 1
            break
        rows.extend(d)
        after = int(d[-1][0])
    if not rows:
        print('no okx', inst, kind, flush=True)
        return
    ncol = len(rows[0])
    cols = ['ts', 'open', 'high', 'low', 'close'] + [f'c{i}' for i in range(5, ncol)]
    df = pd.DataFrame(rows, columns=cols)
    keep = ['ts', 'open', 'high', 'low', 'close'] + (['c5', 'c7'] if kind == 'candles' else [])
    df = df[keep].astype(float)
    if kind == 'candles':
        df = df.rename(columns={'c5': 'vol_contracts', 'c7': 'vol_quote'})
    df = df.drop_duplicates('ts').sort_values('ts')
    df.to_parquet(fn)
    print('okx', coin, kind, len(df), pd.to_datetime(df.ts.iloc[0], unit='ms'), flush=True)


if __name__ == '__main__':
    what = sys.argv[1] if len(sys.argv) > 1 else 'all'
    os.makedirs(D, exist_ok=True)
    if what in ('binance', 'all'):
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda cs: fetch_binance(*cs), UNIVERSE))
    if what in ('okxfunding', 'all'):
        fetch_okx_funding()
    if what in ('okxcandles', 'all'):
        jobs = [(c, k) for k in ('candles', 'mark') for c, _ in UNIVERSE]
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda ck: fetch_okx_candles(*ck), jobs))
    print('done', what, flush=True)


# ---------------- OKX per-instrument monthly funding files (newer layout, complete through 2026) ----------------
def okx_fund_month(coin, month):
    ym = month.replace('-', '')
    inst = f'{coin}-USDT-SWAP'
    url = f'https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/{ym}/{inst}-fundingrates-{month}.zip'
    b = get(url)
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    df.columns = ['instId', 'funding_rate', 'funding_time']
    return df


def fetch_okx_funding2():
    fn = os.path.join(D, 'okx_funding_inst.parquet')
    if os.path.exists(fn):
        return
    months = MONTHS + ['2026-09']
    jobs = [(c, m) for c, _ in UNIVERSE for m in months]
    with ThreadPoolExecutor(12) as ex:
        parts = list(ex.map(lambda cm: okx_fund_month(*cm), jobs))
    miss = [cm for cm, p in zip(jobs, parts) if p is None]
    print('okx inst funding missing', len(miss), miss[:40], flush=True)
    df = pd.concat([p for p in parts if p is not None], ignore_index=True)
    df['funding_time'] = df['funding_time'].astype('int64')
    df = df.drop_duplicates(['instId', 'funding_time']).sort_values(['instId', 'funding_time'])
    df.to_parquet(fn)
    print('okx inst funding rows', len(df), flush=True)


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == 'okxfunding2':
    fetch_okx_funding2()


if __name__ == '__main__' and len(sys.argv) > 2 and sys.argv[1] == 'okxkind':
    kind = sys.argv[2]
    TH['candles'] = Throttle(9.0)
    TH['mark'] = Throttle(4.5)
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda c: fetch_okx_candles(c, kind), [c for c, _ in UNIVERSE]))
    print('done kind', kind, flush=True)
