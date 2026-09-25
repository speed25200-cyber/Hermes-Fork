"""Verifier: Binance UM 1h klines, 2024-12..2026-08, for every coin that was ever in the selected TSMOM universe OOS.
Only t,o,h,l,c kept -> h1/h1_oos.parquet (used for a 1-hour execution-delay stress and an hourly simultaneous trough)."""
import io, ssl, sys, time, zipfile, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_combo_tsmom/rerun')
import tsmom as M
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(url, tries=6):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    return None
P = M.load(); D = P['dates']; S = np.array(P['syms'])
Wt, mem, rank = M.target_weights(dict(N=30, lbs=[10, 20, 40], kind='z', hl=60), {})
t0 = D.searchsorted(pd.Timestamp('2024-12-01', tz='UTC'))
coins = sorted(S[mem[t0:].any(0)])
months = [str(p) for p in pd.period_range('2024-12', '2026-08', freq='M')]
jobs = [(s, m) for s in coins for m in months]
def dl(job):
    s, m = job
    b = get(f'https://data.binance.vision/data/futures/um/monthly/klines/{s}/1h/{s}-1h-{m}.zip')
    if b is None: return job, None
    z = zipfile.ZipFile(io.BytesIO(b))
    d = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None)
    if not str(d.iloc[0, 0]).isdigit(): d = d.iloc[1:]
    d = d.iloc[:, :5].astype(float); d.columns = ['t', 'o', 'h', 'l', 'c']
    d['t'] = d.t.astype('int64'); d['sym'] = s
    return job, d
with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(dl, jobs))
got = [d for _, d in res if d is not None]; miss = [j for j, d in res if d is None]
print('coins', len(coins), 'files', len(got), 'missing', len(miss))
out = pd.concat(got, ignore_index=True)
out.to_parquet('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_combo_tsmom/h1/h1_oos.parquet')
pd.DataFrame(miss, columns=['sym', 'month']).to_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_combo_tsmom/h1/missing.csv', index=False)
