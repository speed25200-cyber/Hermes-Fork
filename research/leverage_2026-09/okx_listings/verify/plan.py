import sys; sys.path.insert(0,'.')
from dl import *
from concurrent.futures import ThreadPoolExecutor
sel=pd.read_csv('sel.csv')
ev=sel.drop_duplicates('sym')[['sym','t0','t0_exact']]
jobs=[]
for _,r in ev.iterrows():
    a=pd.Timestamp(int(r.t0_exact),unit='ms')+pd.Timedelta(hours=8)
    b=pd.Timestamp(int(r.t0),unit='ms')+pd.Timedelta(hours=8+171)
    for d in pd.date_range(a.normalize()-pd.Timedelta(days=1), b.normalize(), freq='D'):
        jobs.append((r.sym,d))
def head(j):
    inst,day=j
    url=f'https://static.okx.com/cdn/okex/traderecords/trades/daily/{day:%Y%m%d}/{inst}-trades-{day:%Y-%m-%d}.zip'
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'curl/8.0'},method='HEAD'),context=CTX,timeout=60) as r:
                return j, int(r.headers.get('Content-Length',0))
        except urllib.error.HTTPError as e:
            if e.code in (403,404): return j, None
        except Exception: time.sleep(2)
    return j,-1
with ThreadPoolExecutor(12) as ex: res=list(ex.map(head,jobs))
df=pd.DataFrame([(i,d.date(),s) for (i,d),s in res],columns=['inst','day8','bytes'])
df.to_csv('okx_plan.csv',index=False)
print(df.groupby('inst').agg(n=('bytes','size'),have=('bytes',lambda x:x.notna().sum()),mb=('bytes',lambda x:x.sum()/1e6)))
print(df[df.bytes.isna()])
