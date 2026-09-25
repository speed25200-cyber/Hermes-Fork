"""Token age and funding for the OKX-listing events.
Age: oldest Binance spot market (S3 archive, any of USDT/BUSD/USDC/FDUSD/BTC/BNB/TRY), oldest OKX spot market (current
instruments' listTime; for markets no longer listed, archive probes of BASE-USDT spot trades 31/60/120/365 days before
t0), and the Bybit perp launch (public archive) for a sensitivity check.
Funding: OKX realized funding (all-swaps daily files to 2025-09-07; per-instrument monthly files after; REST history
for the current month). -> data/ev_age.csv, data/ev_funding.parquet (instId, funding_time, rate)"""
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import *
from concurrent.futures import ThreadPoolExecutor
import re, urllib.parse
suffix = '_all' if '--all' in sys.argv else ''
t0s = pd.read_csv(os.path.join(D, f'ev_t0{suffix}.csv'))
t0s['base'] = t0s.instId.str.replace('-USDT-SWAP', '', regex=False)
S3 = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'


def s3_prefixes(prefix):
    pres, marker = [], ''
    while True:
        u = f'{S3}?prefix={urllib.parse.quote(prefix)}&delimiter=/' + (f'&marker={urllib.parse.quote(marker)}' if marker else '')
        x = get(u).decode()
        p = re.findall(r'<CommonPrefixes><Prefix>(.*?)</Prefix></CommonPrefixes>', x)
        k = re.findall(r'<Key>(.*?)</Key>', x)
        pres += p
        if '<IsTruncated>true</IsTruncated>' not in x:
            return pres, k
        m = re.search(r'<NextMarker>(.*?)</NextMarker>', x)
        marker = m.group(1) if m else (k[-1] if k else p[-1])


spot_syms = set(x.split('/')[-2] for x in s3_prefixes('data/spot/monthly/klines/')[0]) | \
    set(x.split('/')[-2] for x in s3_prefixes('data/spot/daily/klines/')[0])
print('binance spot symbols', len(spot_syms), flush=True)


def bn_spot_first(base):
    best = None
    for q in ('USDT', 'BUSD', 'USDC', 'FDUSD', 'BTC', 'BNB', 'TRY'):
        s = base + q
        if s not in spot_syms:
            continue
        for kind in ('monthly', 'daily'):
            _, keys = s3_prefixes(f'data/spot/{kind}/klines/{s}/1d/')
            ds = sorted(re.findall(r'(\d{4}-\d{2}(?:-\d{2})?)\.zip$', k)[0] for k in keys if k.endswith('.zip'))
            if ds:
                d = pd.Timestamp(ds[0] + ('-01' if len(ds[0]) == 7 else ''))
                best = d if best is None or d < best else best
    return best


spot = json.load(open(os.path.join(D, 'okx_spot_instruments.json')))
okx_spot = {}
for x in spot:
    b = x['baseCcy']
    if x.get('listTime'):
        t = pd.Timestamp(int(x['listTime']), unit='ms')
        okx_spot[b] = min(okx_spot.get(b, t), t)


def okx_spot_old(base, t0):
    """True if a BASE-USDT spot archive file exists 31, 60, 120 or 365 days before t0."""
    for k in (31, 60, 120, 365):
        day = day8(t0) - pd.Timedelta(days=k)
        u = f'{STATIC}/trades/daily/{day:%Y%m%d}/{base}-USDT-trades-{day:%Y-%m-%d}.zip'
        if get(u, head=True):
            return True
    return False


by = json.load(open(os.path.join(BASE, 'bybit_calendar.json')))['trading']
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.data.universe import base_asset
by_first = {}
for s, v in by.items():
    if v and s.endswith('USDT'):
        b = base_asset(s)
        t = pd.Timestamp(v[0])
        by_first[b] = min(by_first.get(b, t), t)


def age(r):
    t0 = pd.Timestamp(int(r.t0), unit='ms')
    return dict(instId=r.instId, bn_spot=bn_spot_first(r.base), okx_spot_live=okx_spot.get(r.base),
                okx_spot_old=okx_spot_old(r.base, r.t0), bybit_perp=by_first.get(r.base))


with ThreadPoolExecutor(12) as ex:
    ages = pd.DataFrame(list(ex.map(age, [r for _, r in t0s.iterrows()])))
ages.to_csv(os.path.join(D, f'ev_age{suffix}.csv'), index=False)
print(ages.notna().sum().to_dict(), int(ages.okx_spot_old.sum()), flush=True)

# funding
a = pd.read_parquet(os.path.join(os.path.dirname(BASE), 'xvenue', 'data', 'okx_funding_all.parquet'))
a = a[a.instId.isin(set(t0s.instId))][['instId', 'funding_time', 'real_funding_rate']].rename(columns={'real_funding_rate': 'rate'})
a = a.sort_values(['instId', 'funding_time'])
iv = a.groupby('instId').funding_time.diff().fillna(8 * 3600000)       # daily files stamp the NEXT settlement
a['funding_time'] = a.funding_time - iv.astype('int64')
jobs = []
for _, r in t0s.iterrows():
    m0 = pd.Timestamp(int(r.t0), unit='ms').to_period('M')
    for k in range(0, 2):
        m = m0 + k
        if str(m) >= '2025-09':
            jobs.append((r.instId, str(m)))


def month(job):
    inst, m = job
    b = get(f'{STATIC}/swaprates/monthly/{m.replace("-", "")}/{inst}-fundingrates-{m}.zip', tries=4)
    if b is None:
        return job, None
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    df.columns = ['instId', 'rate', 'funding_time']
    return job, df


with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(month, jobs))
got = [d for _, d in res if d is not None]
miss = [j for j, d in res if d is None]
rest = []
for inst, m in miss:
    d = okx_get(f'/api/v5/public/funding-rate-history?instId={inst}&limit=100')
    if d:
        rest.append(pd.DataFrame({'instId': inst, 'rate': [float(x['realizedRate'] or x['fundingRate']) for x in d],
                                  'funding_time': [int(x['fundingTime']) for x in d]}))
print('monthly files', len(got), 'missing', len(miss), 'rest', len(rest), flush=True)
fu = pd.concat([a] + got + rest, ignore_index=True)
fu['funding_time'] = fu.funding_time.astype('int64')
fu = fu.drop_duplicates(['instId', 'funding_time']).sort_values(['instId', 'funding_time'])
fu.to_parquet(os.path.join(D, f'ev_funding{suffix}.parquet'))
print('funding rows', len(fu), fu.instId.nunique())
