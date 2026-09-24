"""Verifier step 9: recompute the 4 biggest OOS winning trades of the 1x slow config from FRESH raw Binance archives
(1h perp klines, fundingRate, BTCUSDT 1h klines), independent of the researcher's arrays.
Long coin (s=+1) hedged short BTC with beta from the engine's arrays (clipped to [0.2, 3])."""
import io, os, ssl, zipfile, urllib.request, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=120) as r:
        return r.read()
def zcsv(u, names):
    z = zipfile.ZipFile(io.BytesIO(get(u))); raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=0 if not raw[:1].isdigit() else None)
    df = df.iloc[:, :len(names)]; df.columns = names; return df
def kl(sym, mo):
    return zcsv(f'https://data.binance.vision/data/futures/um/monthly/klines/{sym}/1h/{sym}-1h-{mo}.zip', ['t', 'o', 'h', 'l', 'c'])
def fr(sym, mo):
    return zcsv(f'https://data.binance.vision/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{mo}.csv'.replace('.csv', '.zip'), ['t', 'ih', 'rate'])
z = np.load('/dev/shm/fundevent/slow_arrays.npz')
syms = list(z['syms']); beta = z['A_beta']
H0 = pd.Timestamp('2021-12-01')
TR = [('RAVEUSDT', '2026-04-11 20:00', '2026-04-15 23:00', 0.155645, 1.198048),
      ('MYXUSDT', '2025-08-04 16:00', '2025-08-06 01:00', 0.234271, 1.012726),
      ('ALPACAUSDT', '2025-04-24 16:00', '2025-04-29 12:00', 0.188833, 0.774642),
      ('TAIKOUSDT', '2026-06-23 16:00', '2026-07-02 15:00', 0.205399, 0.659624)]
for sym, a, b, m0, mend in TR:
    ta, tb = pd.Timestamp(a), pd.Timestamp(b)
    mos = sorted({ta.strftime('%Y-%m'), tb.strftime('%Y-%m')})
    k = pd.concat([kl(sym, m) for m in mos]); k['t'] = pd.to_datetime(k.t.astype(np.int64), unit='ms'); k = k.set_index('t')
    kb = pd.concat([kl('BTCUSDT', m) for m in mos]); kb['t'] = pd.to_datetime(kb.t.astype(np.int64), unit='ms'); kb = kb.set_index('t')
    f = pd.concat([fr(sym, m) for m in mos]); f['t'] = pd.to_datetime((f.t.astype(np.int64) // 60000) * 60000, unit='ms')
    fb = pd.concat([fr('BTCUSDT', m) for m in mos]); fb['t'] = pd.to_datetime((fb.t.astype(np.int64) // 60000) * 60000, unit='ms')
    h = int((ta - H0) / pd.Timedelta(hours=1))
    bt = float(np.clip(beta[syms.index(sym), h], 0.2, 3.0)) if np.isfinite(beta[syms.index(sym), h]) else 1.0
    Pe, Px = float(k.o[ta]), float(k.o[tb]); Be, Bx = float(kb.o[ta]), float(kb.o[tb])
    fw = f[(f.t > ta) & (f.t <= tb)]
    fund = float(sum(-r.rate * float(k.o.get(r.t, np.nan)) / Pe for r in fw.itertuples()))
    fbw = fb[(fb.t > ta) & (fb.t <= tb)]
    bfund = float(sum(bt * r.rate * float(kb.o[r.t]) / Be for r in fbw.itertuples()))
    coin = Px / Pe - 1; hed = -bt * (Bx / Be - 1)
    # 1x lowest liquidation-relevant check: worst hourly low vs entry
    seg = k.loc[ta:tb]
    print(f'{sym}: entry {Pe:.5g} exit {Px:.5g} coin {coin:+.3f} | BTC hedge beta {bt:.2f} {hed:+.3f} | funding {len(fw)} settlements {fund:+.3f} '
          f'| BTC funding {bfund:+.4f} | gross {coin + hed + fund + bfund:+.3f} | engine net ret {mend / m0 - 1:+.3f} | min low/entry {seg.l.min() / Pe:.3f} max high/entry {seg.h.max() / Pe:.3f}')
    print('   funding rates (first/last 3):', fw.rate.head(3).round(5).tolist(), fw.rate.tail(3).round(5).tolist(), 'interval', fw.ih.iloc[0] if len(fw) else None)
