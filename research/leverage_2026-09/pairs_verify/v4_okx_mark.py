"""Verifier step 4: Binance vs OKX 1-minute MARK price on the hours that liquidate the high-leverage picks.
For each (hour, pair, beta): W_long / W_short = worst within-minute spread move vs the hour open (same definition as
minute_bound.py, ratio r = beta), from Binance 1m mark (daily archive file) and from OKX
/api/v5/market/history-mark-price-candles (1m). Survivable per-leg leverage for a single-pair cross account as in
shock_fixed.py."""
import io, zipfile, json, ssl, sys, time, urllib.request
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from dl_daily import get
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
tiers = pd.read_csv('data/okx_tiers.csv').set_index('sym')

def bn_1m(sym, day):
    b = get(f'https://data.binance.vision/data/futures/um/daily/markPriceKlines/{sym}/1m/{sym}-1m-{day}.zip')
    if b is None:
        return None
    raw = zipfile.ZipFile(io.BytesIO(b)).read(zipfile.ZipFile(io.BytesIO(b)).namelist()[0])
    d = pd.read_csv(io.BytesIO(raw), header=0 if not raw[:1].isdigit() else None, usecols=[0, 1, 2, 3])
    d.columns = ['t', 'o', 'h', 'l']
    d = d[pd.to_numeric(d.t, errors='coerce').notna()].astype(float)
    return d.set_index('t')

def okx_1m(sym, t0):
    inst = tiers.okx.get(sym) + '-USDT-SWAP'
    u = f'https://www.okx.com/api/v5/market/history-mark-price-candles?instId={inst}&bar=1m&after={t0 + 3600000}&limit=100'
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=30) as r:
                x = json.load(r)
            if x.get('code') == '0':
                d = pd.DataFrame(x['data'], columns=['t', 'o', 'h', 'l', 'c', 'confirm']).astype(float)
                return d[(d.t >= t0) & (d.t < t0 + 3600000)].set_index('t').sort_index()
        except Exception:
            time.sleep(1 + k)
    return None

def w_hour(da, db, t0, r):
    idx = np.arange(t0, t0 + 3600000, 60000)
    if da is None or db is None:
        return np.nan, np.nan, 0
    a = da.reindex(idx); b = db.reindex(idx)
    n = int((a.h.notna() & b.h.notna()).sum())
    oa = a.o.iloc[0]; ob = b.o.iloc[0]
    wl = ((a.l / oa - 1) - r * (b.h / ob - 1)).min()
    ws = (-(a.h / oa - 1) + r * (b.l / ob - 1)).min()
    return wl, ws, n

def lmax(w, be, ma, mb, fee=0.0005):
    mmfac = 2 * ((ma + fee) + be * (mb + fee)) / (1 + be)
    return 1.0 / (mmfac + 2 * (-w) / (1 + be))

if __name__ == '__main__':
    ev = pd.read_csv(sys.argv[1])        # columns: when, a, b, beta
    rows = []
    for e in ev.itertuples():
        t0 = int(pd.Timestamp(e.when).value // 10**6)
        day = str(pd.Timestamp(e.when).date())
        ba, bb = bn_1m(e.a, day), bn_1m(e.b, day)
        oa, ob = okx_1m(e.a, t0), okx_1m(e.b, t0)
        wlb, wsb, nb = w_hour(ba, bb, t0, e.beta)
        wlo, wso, no = w_hour(oa, ob, t0, e.beta)
        ma = tiers.mmr.get(e.a, 0.02); mb = tiers.mmr.get(e.b, 0.02)
        ma = 0.02 if pd.isna(ma) else ma; mb = 0.02 if pd.isna(mb) else mb
        rows.append(dict(when=e.when, pair=f'{e.a}/{e.b}', beta=round(e.beta, 3), bn_min_long=wlb, okx_min_long=wlo,
                         bn_min_short=wsb, okx_min_short=wso, n_bn=nb, n_okx=no,
                         lmax_bn=lmax(min(wlb, wsb), e.beta, ma, mb), lmax_okx=lmax(min(wlo, wso), e.beta, ma, mb)))
        print(rows[-1], flush=True)
    out = pd.DataFrame(rows)
    pd.set_option('display.width', 250)
    print(out.round(4).to_string(index=False))
    out.to_csv(sys.argv[2], index=False)
