"""OKX realized funding for the event instruments, settlement-time stamped.
Primary: per-instrument monthly files (static.okx.com swaprates/monthly/{ym}/{inst}-fundingrates-{month}.zip) for the
months covering each event window. Fallback where a month file is missing: the all-swaps daily files (cached by the
xvenue study; their 'funding_time' is the NEXT settlement -> shifted back by the instrument's interval).
-> data/okx_funding.parquet (instId, funding_time ms, rate, src)"""
from common import *
from concurrent.futures import ThreadPoolExecutor
ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
P = np.load(os.path.join(D, 'panel.npz'))
sel = P['okx_on'].any(1)
jobs = set()
for _, r in ev[sel].iterrows():
    m0 = pd.Timestamp(r.t0, unit='ms').to_period('M')
    for k in range(0, 6):
        m = str(m0 + k)
        if m <= '2026-08':
            jobs.add((r.inst, m))
m0 = pd.Period('2021-12', 'M')
jobs |= {('BTC-USDT-SWAP', str(m0 + k)) for k in range(57)}
jobs = sorted(jobs)

def month(job):
    inst, m = job
    ym = m.replace('-', '')
    b = get(f'https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/{ym}/{inst}-fundingrates-{m}.zip', tries=4)
    if b is None:
        return job, None
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    df.columns = ['instId', 'rate', 'funding_time']
    return job, df

with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(month, jobs))
got = [d for _, d in res if d is not None]
miss = [j for j, d in res if d is None]
print('monthly files', len(got), 'missing', len(miss), miss[:10])
b = pd.concat(got, ignore_index=True)
b['src'] = 'monthly'
a = pd.read_parquet('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/okx_funding_all.parquet')
a = a[a.instId.isin({i for i, _ in miss})][['instId', 'funding_time', 'real_funding_rate']].rename(columns={'real_funding_rate': 'rate'})
a = a.sort_values(['instId', 'funding_time'])
iv = a.groupby('instId').funding_time.diff().fillna(8 * 3600000)
a['funding_time'] = a.funding_time - iv.astype('int64')
a['ym'] = pd.to_datetime(a.funding_time, unit='ms').dt.to_period('M').astype(str)
a = a[[ (i, m) in set(miss) for i, m in zip(a.instId, a.ym)]].drop(columns='ym')
a['src'] = 'daily'
out = pd.concat([b[['instId', 'funding_time', 'rate', 'src']], a], ignore_index=True)
out['funding_time'] = out.funding_time.astype('int64')
out = out.drop_duplicates(['instId', 'funding_time']).sort_values(['instId', 'funding_time'])
out.to_parquet(os.path.join(D, 'okx_funding.parquet'))
print(out.shape, out.instId.nunique(), out.src.value_counts().to_dict())
