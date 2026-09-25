"""All OKX announcements (new listings + delistings) via public REST -> ann.parquet"""
import sys, json
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import get
import pandas as pd, time
rows = []
for typ in ('announcements-new-listings', 'announcements-delistings'):
    for p in range(1, 400):
        b = get(f'https://www.okx.com/api/v5/support/announcements?annType={typ}&page={p}')
        d = json.loads(b)
        if d.get('code') != '0':
            print('err', typ, p, d); time.sleep(2); continue
        det = d['data'][0]['details'] if d['data'] else []
        if not det:
            break
        rows += det
        if p == 1:
            print(typ, 'totalPage', d['data'][0].get('totalPage'))
        time.sleep(0.15)
df = pd.DataFrame(rows)
df['pTime'] = pd.to_datetime(df.pTime.astype('int64'), unit='ms')
df['bTime'] = pd.to_datetime(df.businessPTime.astype('int64'), unit='ms')
df.to_parquet('ann.parquet')
print(len(df), df.pTime.min(), df.pTime.max())
