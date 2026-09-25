import sys, glob; sys.path.insert(0,'.')
from dl import *
import numpy as np
from concurrent.futures import ProcessPoolExecutor
H=3600000
def one(fn):
    df=pd.read_csv(fn,compression='gzip',usecols=['timestamp','price','size'])
    t=(df.timestamp*1000).round().astype('int64')
    h=(t//H)*H
    df=df.assign(t=t,h=h).sort_values('t',kind='stable')
    g=df.groupby('h').price
    b=pd.DataFrame({'o':g.first(),'h':g.max(),'l':g.min(),'c':g.last(),'n':g.size(),'vol':df.groupby('h')['size'].sum()})
    # also keep per-hour 99.9th/0.1th pct and 2nd-highest price to detect single-print spikes
    b['h2']=df.groupby('h').price.apply(lambda x: x.nlargest(5).iloc[-1] if len(x)>=5 else x.max())
    return os.path.basename(fn), b
fns=sorted(glob.glob('cache/bybit/*.csv.gz'))
with ProcessPoolExecutor(6) as ex: res=list(ex.map(one,fns))
out={}
for fn,b in res:
    sym=fn[:-len('2024-11-25.csv.gz')]
    out.setdefault(sym,[]).append(b)
out={s:pd.concat(v).sort_index() for s,v in out.items()}
out={s:v.groupby(level=0).agg(o=('o','first'),h=('h','max'),l=('l','min'),c=('c','last'),n=('n','sum'),vol=('vol','sum'),h2=('h2','max')) for s,v in out.items()}
pd.to_pickle(out,'bybit_bars.pkl')
print({s:len(v) for s,v in out.items()})
