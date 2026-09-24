"""Verifier step 6: sub-minute price path at extreme NEGATIVE-funding settlements (long receives), from raw Binance
aggTrades (downloaded fresh from data.binance.vision). 40 random OOS events (one per symbol, ann <= -400%/yr).
For each event: last trade price before t (P_pre) and VWAP in windows after t, long-side move in bp vs funding received.
Question: is the bar-t 1m open (first trade after t) a price one could exit at, or does the drop happen instantly?"""
import io, os, ssl, zipfile, urllib.request, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
HERE = os.path.dirname(os.path.abspath(__file__))
s = pd.read_csv('/dev/shm/verify_fe/aggsample.csv')
WINS = [(0, 250), (250, 1000), (1000, 3000), (3000, 10000), (10000, 30000), (30000, 60000)]


def get(url):
    for k in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 + 2 * k)
        except Exception:
            time.sleep(2 + 2 * k)
    return None


def one(r):
    url = f'https://data.binance.vision/data/futures/um/daily/aggTrades/{r.sym}/{r.sym}-aggTrades-{r.day}.zip'
    b = get(url)
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    hdr = 0 if not raw[:1].isdigit() else None
    df = pd.read_csv(io.BytesIO(raw), header=hdr, usecols=[1, 2, 5])
    df.columns = ['p', 'q', 'ts']
    ts = df.ts.values.astype(np.int64)
    ts = np.where(ts > 10 ** 14, ts // 1000, ts)
    t = int(r.t)
    m = (ts >= t - 120000) & (ts < t + 120000)
    p, q, ts = df.p.values[m].astype(float), df.q.values[m].astype(float), ts[m]
    pre = ts < t
    rec = {'sym': r.sym, 't': t, 'mb': len(b) / 1e6, 'fund_bp': -r.rate * 1e4, 'n_pre60s': int(((ts >= t - 60000) & pre).sum())}
    P0 = p[pre][-1] if pre.any() else np.nan
    rec['vwap_pre_10s'] = np.nan
    mm = (ts >= t - 10000) & pre
    if mm.any():
        rec['vwap_pre_10s'] = np.average(p[mm], weights=q[mm])
    rec['first_after'] = p[~pre][0] if (~pre).any() else np.nan
    rec['first_after_ms'] = int(ts[~pre][0] - t) if (~pre).any() else -1
    for a, bb in WINS:
        mm = (ts >= t + a) & (ts < t + bb)
        rec[f'n_{a}_{bb}'] = int(mm.sum())
        rec[f'mv_{a}_{bb}'] = (np.log(np.average(p[mm], weights=q[mm]) / P0) * 1e4) if mm.any() else np.nan
        rec[f'usd_{a}_{bb}'] = float((p[mm] * q[mm]).sum())
    rec['mv_first'] = np.log(rec['first_after'] / P0) * 1e4
    return rec


rows = []
with ThreadPoolExecutor(4) as ex:
    for x in ex.map(one, s.itertuples()):
        if x is not None:
            rows.append(x)
            print(x['sym'], 'fund %.1f first %+.1f (%d ms) | ' % (x['fund_bp'], x['mv_first'], x['first_after_ms']) +
                  ' '.join(f"{a}-{b}ms:{x[f'mv_{a}_{b}']:+.1f}" for a, b in WINS), flush=True)
df = pd.DataFrame(rows)
df.to_csv(os.path.join(HERE, 'v6_aggtrades.csv'), index=False)
print('events', len(df))
cols = ['fund_bp', 'mv_first'] + [f'mv_{a}_{b}' for a, b in WINS]
print('mean bp (long side, vs last trade before t):'); print(df[cols].mean().round(1).to_string())
print('median bp:'); print(df[cols].median().round(1).to_string())
print('ratio move/funding (mean of per-event):')
for c in cols[1:]:
    print(c, round(float((df[c] / df.fund_bp).median()), 2))
print('median USD traded per window:', {f'{a}-{b}': round(float(df[f"usd_{a}_{b}"].median())) for a, b in WINS})
