"""OKX point-in-time listing calendar for every Binance USDT perp (delisted included on both venues), from OKX's
per-instrument daily trade archives (static.okx.com; a file exists for day D iff the swap traded on D, UTC+8 days).
Uses Hermes' OkxListing (read-only import) with its grid+bisection over each symbol's Binance life, plus DENSE daily
probes from Binance listing day -3 to +35 so the start of OKX trading around a new listing is exact to the archive day.
-> data/okx_calendar.parquet (day x symbol bool), data/okx_first.csv"""
import sys
sys.path.insert(0, '/home/user/Hermes/src')
from common import *
import httpx, logging
from datetime import date, timedelta
from hermes.data.venue import OkxListing
from hermes.execution.okx.instruments import okx_inst_id
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
dk = pd.read_parquet(os.path.join(D, 'daily_klines.parquet'))
dk['d'] = pd.to_datetime(dk.t, unit='ms')
g = dk.groupby('sym').d.agg(['min', 'max'])
windows = {s: (r['min'].date(), r['max'].date()) for s, r in g.iterrows()}
client = httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0), follow_redirects=True, verify=CTX)
L = OkxListing(D, client=client, workers=48)
# dense probes around each Binance listing since 2021-12
dense = []
for s, (a, b) in windows.items():
    if a < date(2021, 12, 4):
        continue
    inst = okx_inst_id(s)
    for k in range(-3, 36):
        d = a + timedelta(days=k)
        if date(2021, 12, 1) <= d <= min(b, date(2026, 9, 20)):
            dense.append((inst, d))
L._probe(dense)
cal = L.calendar(windows)
cal.to_parquet(os.path.join(D, 'okx_calendar.parquet'))
rows = []
for s, (a, b) in windows.items():
    c = cal[s]
    on = c[c]
    rows.append((s, okx_inst_id(s), a, b, on.index[0].date() if len(on) else None, on.index[-1].date() if len(on) else None, int(c.sum())))
pd.DataFrame(rows, columns=['sym', 'inst', 'bn_first', 'bn_last', 'okx_first', 'okx_last', 'okx_days']).to_csv(os.path.join(D, 'okx_first.csv'), index=False)
print('done', cal.shape, int(cal.values.sum()))
