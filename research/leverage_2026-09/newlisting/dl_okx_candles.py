"""OKX 1H last-price candles (public REST history-candles) for every event whose OKX contract is listed today
(crypto category), over the event's first 100 days. Delisted OKX contracts are not served by the REST API: those
events keep Binance prices (see sim). -> data/okx_h1.parquet (inst, t, o, h, l, c in OKX units)"""
import sys
sys.path.insert(0, '/home/user/Hermes/src')
from common import *
from concurrent.futures import ThreadPoolExecutor
from hermes.execution.okx.instruments import okx_inst_id
ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
P = np.load(os.path.join(D, 'panel2.npz'))
inst = {x['instId']: x for x in json.load(open(os.path.join(D, 'okx_instruments.json')))}
jobs = []
for i, r in ev.iterrows():
    iid = okx_inst_id(r.sym)
    if P['okx_on'][i].any() and iid in inst and inst[iid].get('instCategory') == '1':
        jobs.append((r.sym, iid, int(r.t0), int(r.t0) + 100 * 86400000))
print(len(jobs), 'events', flush=True)


def fetch(job):
    sym, iid, a, b = job
    rows, after = [], min(b, int(time.time() * 1000)) + 3600000
    for _ in range(40):
        d = okx_get(f'/api/v5/market/history-candles?instId={iid}&bar=1H&limit=100&after={after}')
        if not isinstance(d, list) or not d:
            break
        rows += d
        after = int(d[-1][0])
        if after <= a:
            break
        time.sleep(0.25)
    if not rows:
        return None
    df = pd.DataFrame([[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in rows], columns=['t', 'o', 'h', 'l', 'c'])
    df = df[(df.t >= a - 3 * 86400000) & (df.t <= b)].drop_duplicates('t')
    df['inst'], df['sym'] = iid, sym
    return df


with ThreadPoolExecutor(3) as ex:
    parts = [p for p in ex.map(fetch, jobs) if p is not None]
out = pd.concat(parts, ignore_index=True)
out.to_parquet(os.path.join(D, 'okx_h1.parquet'))
print(out.shape, out.sym.nunique())
