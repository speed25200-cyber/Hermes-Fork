import sys; sys.path.insert(0,'.')
from dl import *
from concurrent.futures import ThreadPoolExecutor
import json
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
by=json.load(open(f'{SP}/bybit_calendar.json'))['trading']
m={'MEMEFI-USDT-SWAP':'MEMEFIUSDT','GRASS-USDT-SWAP':'GRASSUSDT','FITFI-USDT-SWAP':'FITFIUSDT','PYTH-USDT-SWAP':'PYTHUSDT',
   'ZEUS-USDT-SWAP':'ZEUSUSDT','GPT-USDT-SWAP':'GPTUSDT','PEPE-USDT-SWAP':'1000PEPEUSDT','GRIFFAIN-USDT-SWAP':'GRIFFAINUSDT',
   'MORPHO-USDT-SWAP':'MORPHOUSDT','ZEREBRO-USDT-SWAP':'ZEREBROUSDT','DUCK-USDT-SWAP':'DUCKUSDT','NC-USDT-SWAP':'NCUSDT',
   'BUZZ-USDT-SWAP':'BUZZUSDT','CP-USDT-SWAP':'CPUSDT','DOS-USDT-SWAP':'DOSUSDT'}
sel=pd.read_csv('sel.csv')
jobs=[]
for inst,s in m.items():
    r=sel[sel.sym==inst]
    a=pd.Timestamp(int(r.t0.iloc[0]),unit='ms').normalize(); b=pd.Timestamp(int(r.t0.iloc[0]),unit='ms')+pd.Timedelta(hours=170)
    first=pd.Timestamp(by[s][0])
    for d in pd.date_range(max(a,first),b.normalize(),freq='D'): jobs.append((s,d))
print(len(jobs))
def one(j):
    s,d=j
    try: return j, bybit_day(s,d) is not None
    except Exception as e: return j, None
with ThreadPoolExecutor(8) as ex: res=list(ex.map(one,jobs))
print(sum(bool(x) for _,x in res), [j for j,x in res if not x])
