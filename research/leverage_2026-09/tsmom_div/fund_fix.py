"""Complete Binance funding for every symbol-month in which a coin is a member of the widest universe (N=40),
2021-09..2026-08. Sources: earlier caches (pairs/data/f, longtail/data/f, newlisting funding) + Binance monthly
fundingRate archives for the remaining symbol-months. -> funding_all.parquet (t ms, rate, sym); rewrites panel F."""
import io, os, ssl, time, zipfile, urllib.request, urllib.error, glob
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
import tsmom as M

SP = M.SP
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')


def get(url, tries=6):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}), context=CTX,
                                        timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    return None


parts = []
for src in ['pairs/data/f', 'longtail/data/f']:
    for f in glob.glob(f'{SP}/{src}/*.parquet'):
        d = pd.read_parquet(f)
        d['sym'] = os.path.basename(f)[:-8]
        parts.append(d[['t', 'rate', 'sym']])
nl = pd.read_parquet(f'{SP}/newlisting/data/funding.parquet')
parts.append(nl[['t', 'rate', 'sym']])
fu = pd.concat(parts, ignore_index=True)
fu['t'] = fu.t.astype('int64')
fu = fu.drop_duplicates(['sym', 't'])

P = M.load()
D = P['dates']; S = np.array(P['syms'])
mem, _ = M.universe(40)
t0 = D.searchsorted(pd.Timestamp('2021-09-01', tz='UTC'))
ym = D.strftime('%Y-%m')
have = set(zip(fu.sym, pd.to_datetime(fu.t, unit='ms', utc=True).dt.strftime('%Y-%m')))
need = set()
for t in range(t0, len(D)):
    for j in np.where(mem[t])[0]:
        if (S[j], ym[t]) not in have:
            need.add((S[j], ym[t]))
need = sorted(need)
print('symbol-months missing funding:', len(need))


def dl(job):
    s, m = job
    b = get(f'https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip')
    if b is None:
        return job, None
    z = zipfile.ZipFile(io.BytesIO(b))
    d = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    d = d.rename(columns={'calc_time': 't', 'last_funding_rate': 'rate'})
    d['sym'] = s
    return job, d[['t', 'rate', 'sym']]


with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(dl, need))
got = [d for _, d in res if d is not None]
miss = [j for j, d in res if d is None]
print('downloaded', len(got), 'still missing', len(miss), miss[:20])
if got:
    fu = pd.concat([fu] + got, ignore_index=True)
fu['t'] = fu.t.astype('int64')
fu = fu.drop_duplicates(['sym', 't']).sort_values(['sym', 't'])
fu.to_parquet(f'{M.W}/funding_all.parquet')
pd.DataFrame(miss, columns=['sym', 'month']).to_csv(f'{M.W}/funding_missing.csv', index=False)

# rebuild F in the panel
fu = fu[fu.sym.isin(S)]
fu['d'] = pd.to_datetime(fu.t - 5 * 60 * 1000, unit='ms', utc=True).dt.normalize()
F = fu.groupby(['d', 'sym']).rate.sum().unstack().reindex(index=D, columns=S).fillna(0.0)
z = dict(np.load(f'{M.W}/panel.npz', allow_pickle=False))
z['F'] = F.values
np.savez_compressed(f'{M.W}/panel.npz', **z)
m = mem[t0:]
f = F.values[t0:]
print('member-days with zero funding after fix:', int(((f == 0) & m).sum()), 'of', int(m.sum()))
