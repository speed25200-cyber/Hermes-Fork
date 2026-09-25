import sys; sys.path.insert(0,'.')
from dl import *
from concurrent.futures import ThreadPoolExecutor
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
ev=pd.read_parquet(f'{SP}/data/sim/events.parquet')
ev=ev[ev.t0<pd.Timestamp('2025-09-01').value//10**6]
jobs=[]
for _,r in ev.iterrows():
    m0=pd.Timestamp(int(r.t0),unit='ms').to_period('M')
    for k in (0,1): jobs.append((r.sym,str(m0+k)))
def one(j):
    inst,m=j
    fn=fetch(f'https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/{m.replace("-","")}/{inst}-fundingrates-{m}.zip', os.path.join(V,'cache','fund',f'{inst}-{m}.zip'))
    if fn is None: return None
    z=zipfile.ZipFile(fn); df=pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    df.columns=['instId','rate','funding_time']; return df
with ThreadPoolExecutor(8) as ex: res=list(ex.map(one,jobs))
print('files',sum(r is not None for r in res),'of',len(jobs))
m=pd.concat([r for r in res if r is not None]).drop_duplicates(['instId','funding_time'])
m.to_parquet('fund_monthly.parquet')
fu=pd.read_parquet(f'{SP}/data/ev_funding.parquet')
H=3600000
rows=[]
for inst,g in m.groupby('instId'):
    a=fu[(fu.instId==inst)&(fu.funding_time>=g.funding_time.min())&(fu.funding_time<=g.funding_time.max())].set_index('funding_time').rate
    b=g.set_index('funding_time').rate
    j=pd.concat([a.rename('a'),b.rename('b')],axis=1)
    both=j.dropna()
    rows.append(dict(inst=inst,n_month=len(b),n_author=len(a),matched=len(both),maxdiff=float((both.a-both.b).abs().max()) if len(both) else np.nan,
                     month_only=int(j.a.isna().sum()),author_only=int(j.b.isna().sum())))
d=pd.DataFrame(rows); d.to_csv('fund_month_cmp.csv',index=False)
print(d.describe().round(4).to_string())
print(d[(d.month_only>0)|(d.author_only>0)|(d.maxdiff>1e-9)].to_string())
