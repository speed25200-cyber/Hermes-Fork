import sys; sys.path.insert(0,'.')
from dl import *
from concurrent.futures import ThreadPoolExecutor
p=pd.read_csv('okx_plan.csv'); p=p[p.bytes.notna()]
def one(r): 
    fn=okx_day(r.inst, r.day8); return r.inst, r.day8, fn is not None
with ThreadPoolExecutor(8) as ex: res=list(ex.map(one,[r for _,r in p.iterrows()]))
print(sum(x[2] for x in res), len(res))
