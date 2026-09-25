"""OKX 1H last-price and mark-price candles for the book universe on the stress days (2025-10-10, 2025-02-03), to
compare the book bounds (per unit gross) computed from Binance bars with OKX's own prices (liquidation uses mark price).
-> okx_stress_h1.parquet, printed comparison"""
import json, ssl, time, urllib.request
import numpy as np, pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def okx(path):
    for k in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request('https://www.okx.com' + path, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=30) as r:
                j = json.loads(r.read())
            if j.get('code') == '0':
                return j['data']
            time.sleep(1.5)
        except Exception:
            time.sleep(1 + k)
    return None
U = pd.read_parquet('universe_daily.parquet'); U.index = U.index.tz_localize(None)
def inst(s):
    b = s[:-4]
    for p in ('1000000', '1000', '1M'):
        if b.startswith(p) and len(b) > len(p) + 1:
            b = b[len(p):]
            break
    return f'{b}-USDT-SWAP'
rows = []
for d in ('2025-10-10', '2025-02-03'):
    t0 = int(pd.Timestamp(d).value // 10**6)
    for s in U.columns[U.loc[d].values]:
        for kind, ep in (('last', 'history-candles'), ('mark', 'history-mark-price-candles')):
            x = okx(f'/api/v5/market/{ep}?instId={inst(s)}&bar=1H&after={t0 + 24 * 3600000}&before={t0 - 1}&limit=100')
            time.sleep(0.25)
            if not x:
                continue
            for c in x:
                rows.append(dict(day=d, sym=s, kind=kind, t=int(c[0]), o=float(c[1]), h=float(c[2]), l=float(c[3]), c=float(c[4])))
df = pd.DataFrame(rows)
df.to_parquet('okx_stress_h1.parquet')
h1 = pd.read_parquet('h1_universe.parquet')
out = {}
rng = np.random.default_rng(3)
for d in ('2025-10-10', '2025-02-03'):
    t0 = int(pd.Timestamp(d).value // 10**6)
    for kind in ('binance', 'last', 'mark'):
        x = h1[(h1.t >= t0) & (h1.t < t0 + 86400000)].assign(kind='binance') if kind == 'binance' else df[(df.day == d) & (df.kind == kind)]
        mem = sorted(set(df[(df.day == d) & (df.kind == 'mark')].sym))    # names OKX still serves
        x = x[x.sym.isin(mem)]
        o0 = x[x.t == t0].set_index('sym').o
        x = x.assign(lo=x.l / x.sym.map(o0) - 1, hi=x.h / x.sym.map(o0) - 1, hr=(x.t - t0) // 3600000)
        LO = x.pivot(index='hr', columns='sym', values='lo').values; HI = x.pivot(index='hr', columns='sym', values='hi').values
        n = LO.shape[1]; k = min(13, n // 2)
        sL, sH = np.sort(LO, 1), np.sort(HI, 1)
        A = 0.5 * sL[:, :k].mean(1) - 0.5 * sH[:, -k:].mean(1)
        Bv = np.minimum(0.5 * (sL[:, :k].mean(1) - sL[:, -k:].mean(1)), 0.5 * (sH[:, :k].mean(1) - sH[:, -k:].mean(1)))
        perm = np.argsort(rng.random((4000, n)), 1); lg, sh = perm[:, :k], perm[:, k:2 * k]
        rb = lambda X: 0.5 * (X[:, lg].mean(2) - X[:, sh].mean(2))
        C = np.quantile(np.minimum(rb(LO), rb(HI)), 0.01, axis=1)
        M = 0.5 * LO.mean(1) - 0.5 * HI.mean(1)
        out[(d, kind)] = dict(n=n, k=k, A=A.min(), B=Bv.min(), C=C.min(), M=M.min(), mean_low=LO.min(0).mean(), worst_low=LO.min(), btc_low=float(x[x.sym == 'BTCUSDT'].lo.min()) if 'BTCUSDT' in mem else np.nan)
print(pd.DataFrame(out).T.round(4).to_string())
