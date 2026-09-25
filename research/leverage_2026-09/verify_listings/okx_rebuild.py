"""Rebuild OKX 1h last-price OHLC from OKX daily trade archives (static.okx.com, covers delisted contracts) for
events traded by the selected config that have no OKX REST candles. Covers event hours [d0-24, d1+24].
-> okx_rebuilt_h1.parquet (sym, t, o, h, l, c, n)"""
import sys, io, zipfile, json, time, ssl, urllib.request
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.execution.okx.instruments import binance_price_factor
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
URL = "https://static.okx.com/cdn/okex/traderecords/trades/daily/{ymd}/{inst}-trades-{day}.zip"
which = sys.argv[1] if len(sys.argv) > 1 else 'oos'
Dt = Data('hybrid')
ev = Dt.ev
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
start, end = (OOS_START, OOS_END) if which == 'oos' else (IS_START, OOS_START)
gs, ge = gh(start), gh(end)
ent = Dt.g0 + cfg['d0']
need = []
for i in range(Dt.n):
    if not (gs <= ent[i] < ge) or not Dt.newtok[i]:
        continue
    k = cfg['d0']
    if not Dt.okx_on[i, k]:
        continue
    if Dt.src_okx[i, k:cfg['d1']].any():
        continue
    need.append(i)
print(which, 'events needing rebuild', len(need), [ev.sym.values[i] for i in need], flush=True)
jobs = []
for i in need:
    t0 = pd.Timestamp(int(ev.t0.values[i]), unit='ms')
    a = t0 + pd.Timedelta(hours=cfg['d0'] - 24) + pd.Timedelta(hours=8)
    b = t0 + pd.Timedelta(hours=cfg['d1'] + 24) + pd.Timedelta(hours=8)
    for d in pd.date_range(a.normalize(), b.normalize(), freq='D'):
        jobs.append((i, ev.sym.values[i], ev.inst.values[i], d))

def fetch(job):
    i, sym, inst, d = job
    url = URL.format(ymd=d.strftime('%Y%m%d'), inst=inst, day=d.strftime('%Y-%m-%d'))
    for k in range(6):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
            with urllib.request.urlopen(req, context=CTX, timeout=180) as r:
                b = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return job, None, 404
            time.sleep(2 + 3 * k)
        except Exception as e:
            time.sleep(2 + 3 * k)
    else:
        return job, None, 'fail'
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(z.open(z.namelist()[0]), usecols=['trade_id', 'price', 'created_time'])
    df = df.sort_values(['created_time', 'trade_id'])
    h = (df.created_time // 3600000) * 3600000
    g = df.groupby(h).price.agg(['first', 'max', 'min', 'last', 'size'])
    g.columns = ['o', 'h', 'l', 'c', 'n']
    g.index.name = 't'
    g = g.reset_index()
    g['sym'] = sym
    return job, g, len(b)

parts, miss = [], []
with ThreadPoolExecutor(4) as ex:
    for job, g, info in ex.map(fetch, jobs):
        if g is None:
            miss.append((job[1], str(job[3].date()), info))
        else:
            parts.append(g)
print('files', len(jobs), 'missing', miss, flush=True)
out = pd.concat(parts, ignore_index=True)
# an hour split across two UTC+8 day files cannot happen (day boundary = 16:00 UTC, on the hour); still, merge defensively
out = out.sort_values(['sym', 't']).groupby(['sym', 't']).agg(o=('o', 'first'), h=('h', 'max'), l=('l', 'min'), c=('c', 'last'), n=('n', 'sum')).reset_index()
out['factor'] = out.sym.map(binance_price_factor)
out.to_parquet(f'okx_rebuilt_h1_{which}.parquet')
print(out.shape, out.sym.nunique())
