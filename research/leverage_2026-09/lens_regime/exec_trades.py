"""(e) Executability on OKX from OKX's own trade archives (static.okx.com daily files, UTC+8 days) for the OOS
events (2025-01..2026-08) of the 3d->7d and 1d->7d windows whose contracts are still listed (ctVal known):
the UTC+8 day containing the entry hour and the day containing the exit hour. Per file: USDT volume, trades,
quoted-spread proxy (median |price gap| between consecutive opposite-side trades <= 1 s apart), and taker SELL sweep
impact by order size (fills with the same timestamp and side = one taker order; impact = |last/first - 1|).
Also: USDT volume in the entry hour itself. -> exec_trades.json, exec_trades_rows.parquet"""
import sys, io, zipfile, json, time, ssl, urllib.request
import numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
URL = "https://static.okx.com/cdn/okex/traderecords/trades/daily/{ymd}/{inst}-trades-{day}.zip"
inst = {x['instId']: x for x in json.load(open('../newlisting/data/okx_instruments.json'))}
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.execution.okx.instruments import okx_inst_id
X = pd.read_parquet('events_d7.parquet')
X = X[X.t >= '2025-01-01']
jobs = []
for r in X.itertuples():
    iid = okx_inst_id(r.sym)
    if iid not in inst:
        continue
    ctv = float(inst[iid]['ctVal'])
    t_ent = pd.Timestamp(r.t)
    t_exit = t_ent + pd.Timedelta(hours=168 - r.d0)
    for tag, tt in (('entry', t_ent), ('exit', t_exit)):
        d = (tt + pd.Timedelta(hours=8)).normalize()
        jobs.append(dict(sym=r.sym, inst=iid, d0=r.d0, tag=tag, t=tt, day=d, ctv=ctv))
J = pd.DataFrame(jobs).drop_duplicates(['inst', 'day'])
print('files', len(J), flush=True)
BK = [(0, 50), (50, 200), (200, 500), (500, 1000), (1000, 2000), (2000, 5000), (5000, 1e12)]

def fetch(job):
    url = URL.format(ymd=job['day'].strftime('%Y%m%d'), inst=job['inst'], day=job['day'].strftime('%Y-%m-%d'))
    for k in range(6):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
            with urllib.request.urlopen(req, context=CTX, timeout=180) as r:
                b = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return job, None
            time.sleep(2 + 3 * k)
        except Exception:
            time.sleep(2 + 3 * k)
    else:
        return job, None
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(z.open(z.namelist()[0]), usecols=['trade_id', 'side', 'price', 'size', 'created_time'])
    df = df.sort_values(['created_time', 'trade_id']).reset_index(drop=True)
    df['usd'] = df['size'] * job['ctv'] * df.price
    out = dict(vol_usd=float(df.usd.sum()), n_trades=int(len(df)), med_trade_usd=float(df.usd.median()))
    # spread proxy
    s = (df.side == 'buy').values
    p = df.price.values; t = df.created_time.values
    sw = (s[1:] != s[:-1]) & ((t[1:] - t[:-1]) <= 1000)
    gap = np.abs(p[1:] - p[:-1]) / p[:-1]
    out['spread_proxy_bp'] = float(np.median(gap[sw]) * 1e4) if sw.any() else np.nan
    out['spread_proxy_nonzero_bp'] = float(np.median(gap[sw & (gap > 0)]) * 1e4) if (sw & (gap > 0)).any() else np.nan
    # taker sweeps
    g = df.groupby(['created_time', 'side']).agg(first=('price', 'first'), last=('price', 'last'), usd=('usd', 'sum'), n=('price', 'size')).reset_index()
    g['imp_bp'] = np.abs(g['last'] / g['first'] - 1) * 1e4
    sells = g[g.side == 'sell']
    for a, b_ in BK:
        m = (sells.usd >= a) & (sells.usd < b_)
        out[f'sell_{int(a)}_n'] = int(m.sum())
        out[f'sell_{int(a)}_imp_med_bp'] = float(sells.imp_bp[m].median()) if m.any() else np.nan
        out[f'sell_{int(a)}_imp_p90_bp'] = float(sells.imp_bp[m].quantile(0.9)) if m.any() else np.nan
    # entry/exit hour volume
    h0 = int(job['t'].value // 10 ** 6)
    mh = (df.created_time >= h0) & (df.created_time < h0 + 3600000)
    out['hour_vol_usd'] = float(df.usd[mh].sum())
    return job, out

rows = []
with ThreadPoolExecutor(4) as ex:
    for job, out in ex.map(fetch, J.to_dict('records')):
        if out is None:
            rows.append(dict(job, missing=True))
            continue
        rows.append(dict(job, **out, missing=False))
        if len(rows) % 20 == 0:
            print(len(rows), flush=True)
R = pd.DataFrame(rows)
R.to_parquet('exec_trades_rows.parquet')
Y = R[~R.missing]
summ = {'files': int(len(R)), 'missing': int(R.missing.sum())}
for tag in ('entry', 'exit'):
    Z = Y[Y.tag == tag]
    d = dict(n=int(len(Z)), day_vol_usd_q={str(q): float(Z.vol_usd.quantile(q)) for q in (0.1, 0.25, 0.5, 0.75)},
             hour_vol_usd_q={str(q): float(Z.hour_vol_usd.quantile(q)) for q in (0.1, 0.25, 0.5, 0.75)},
             spread_proxy_bp_med=float(Z.spread_proxy_bp.median()), spread_proxy_bp_p90=float(Z.spread_proxy_bp.quantile(0.9)),
             spread_nonzero_bp_med=float(Z.spread_proxy_nonzero_bp.median()), spread_nonzero_bp_p90=float(Z.spread_proxy_nonzero_bp.quantile(0.9)))
    for a, _ in BK:
        d[f'sell_{int(a)}+_impact_med_of_medians_bp'] = float(Z[f'sell_{int(a)}_imp_med_bp'].median())
        d[f'sell_{int(a)}+_impact_med_of_p90_bp'] = float(Z[f'sell_{int(a)}_imp_p90_bp'].median())
        d[f'sell_{int(a)}+_orders_total'] = int(Z[f'sell_{int(a)}_n'].sum())
    summ[tag] = d
json.dump(summ, open('exec_trades.json', 'w'), indent=1)
print(json.dumps(summ, indent=1))
