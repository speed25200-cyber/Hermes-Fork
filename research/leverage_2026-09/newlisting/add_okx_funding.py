"""Add OKX funding (settled in (t, t+1h] -> bar t) to the event panel as fund_okx; hours with no OKX funding record in
the event's OKX funding coverage fall back to Binance funding. -> data/panel2.npz"""
from common import *
ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
P = dict(np.load(os.path.join(D, 'panel.npz')))
fo = pd.read_parquet(os.path.join(D, 'okx_funding.parquet'))
fo = fo[fo.rate.notna()]
g = dict(tuple(fo.groupby('instId')))
n, H = P['o'].shape
F = P['fund'].copy()
src = np.zeros(n, np.int8)
for i, r in ev.iterrows():
    if r.inst not in g or not P['okx_on'][i].any():
        continue
    f = g[r.inst]
    idx = np.ceil((f.funding_time.values - r.t0) / 3600000).astype(np.int64) - 1
    ok = (idx >= 0) & (idx < H)
    if not ok.any():
        continue
    lo, hi = idx[ok].min(), idx[ok].max()
    row = np.zeros(H)
    np.add.at(row, idx[ok], f.rate.values[ok])
    F[i, lo:hi + 1] = row[lo:hi + 1]     # OKX coverage window replaces Binance funding
    src[i] = 1
P['fund_okx'] = F
P['fund_src_okx'] = src
np.savez_compressed(os.path.join(D, 'panel2.npz'), **P)
print('events with OKX funding', int(src.sum()))
