"""Independent pre-market check for the 15 Binance events: Binance spot first daily-kline file (data.binance.vision S3
listing) and OKX spot listTime (REST) vs Binance perp t0."""
import re, json, sys, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist')
from common import get, okx_get
from concurrent.futures import ThreadPoolExecutor
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
ev = pd.read_parquet(SP + '/newlisting/data/events.parquet')
pm = json.load(open(SP + '/xlist/verify/critic/c1_spot.json'))['pm']
def s3_first(sym):
    u = f'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/spot/daily/klines/{sym}/1h/'
    t = (get(u) or b'').decode()
    days = sorted(set(re.findall(r'-1h-(\d{4}-\d{2}-\d{2})\.zip<', t)))
    return days[0] if days else None
with ThreadPoolExecutor(8) as ex:
    first = dict(zip(pm, ex.map(s3_first, pm)))
okx = okx_get('/api/v5/public/instruments?instType=SPOT')
okx_spot = {d['instId']: int(d['listTime']) for d in okx if d['instId'].endswith('-USDT')}
rows = []
for s in pm:
    r = ev[ev.sym == s].iloc[0]
    t0 = pd.to_datetime(r.t0, unit='ms')
    b = s[:-4]
    osp = okx_spot.get(f'{b}-USDT')
    osp = pd.to_datetime(osp, unit='ms') if osp else None
    bsp = pd.Timestamp(first[s]) if first[s] else None
    rows.append(dict(sym=s, perp_t0=t0, bn_spot_first_day=bsp, gap_bn_h=(bsp - t0.floor('D')) / pd.Timedelta('1h') if bsp is not None else None,
                     okx_spot_list=osp, gap_okx_h=round((osp - t0) / pd.Timedelta('1h'), 1) if osp is not None else None,
                     spot_first_in_events=r.spot_first))
df = pd.DataFrame(rows)
print(df.to_string())
df.to_csv(SP + '/xlist/verify/critic/c2_pm_check.csv', index=False)
