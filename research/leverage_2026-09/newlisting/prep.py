"""Build the event table and per-event hourly arrays.
events.parquet: one row per Binance USDT perp listed 2021-12-01..2026-08-31 (delisted included):
  t0 (first 1h bar open, ms), bn_last, okx_first/okx_last (archive days), okx_cat (current OKX category or NaN),
  spot_first (Binance spot, any quote), okx_listTime (current OKX instruments only).
panel.npz: per event hourly arrays from t0 for 150 days: o,h,l,c (NaN after delisting), fund (sum of funding rates
  settled in (t, t+1h]), btc o/h/l/c on the same clock; okx_on (bool per hour: OKX archive exists for the UTC+8 day
  of that hour AND for the previous one -> conservatively, OKX perp had been trading >= ~1 day)."""
import sys
sys.path.insert(0, '/home/user/Hermes/src')
from common import *
from hermes.execution.okx.instruments import okx_inst_id
H = 150 * 24
h1 = pd.read_parquet(os.path.join(D, 'h1.parquet'))
fu = pd.read_parquet(os.path.join(D, 'funding.parquet'))
o = pd.read_csv(os.path.join(D, 'okx_first.csv'), parse_dates=['bn_first', 'bn_last', 'okx_first', 'okx_last'])
sp = pd.read_csv(os.path.join(D, 'spot_first.csv'), parse_dates=['spot_first'])
cal = pd.read_parquet(os.path.join(D, 'okx_calendar.parquet'))
cal.index = cal.index.tz_convert(None)
inst = json.load(open(os.path.join(D, 'okx_instruments.json')))
im = {x['instId']: x for x in inst}
t0 = h1.groupby('sym').t.min()
ev = o.merge(sp[['sym', 'base', 'spot_first']], on='sym')
ev['t0'] = ev.sym.map(t0)
ev = ev[ev.t0.notna() & (ev.bn_first >= '2021-12-01') & (ev.bn_first <= '2026-08-31') & (ev.sym != 'BTCUSDT')].copy()
ev['t0'] = ev.t0.astype('int64')
ev['okx_cat'] = ev.inst.map(lambda i: im[i].get('instCategory') if i in im else None)
ev['okx_listTime'] = ev.inst.map(lambda i: int(im[i]['listTime']) if i in im and im[i].get('listTime') else np.nan)
ev = ev.sort_values('t0').reset_index(drop=True)
btc = h1[h1.sym == 'BTCUSDT'].set_index('t')
g = dict(tuple(h1.groupby('sym')))
gf = dict(tuple(fu.groupby('sym')))
n = len(ev)
A = {k: np.full((n, H), np.nan) for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc')}
A['fund'] = np.zeros((n, H))
A['okx_on'] = np.zeros((n, H), bool)
for i, r in ev.iterrows():
    clock = r.t0 + np.arange(H, dtype=np.int64) * 3600000
    k = g[r.sym].set_index('t').reindex(clock)
    for a in 'ohlc':
        A[a][i] = k[a].values
    b = btc.reindex(clock)
    for a in 'ohlc':
        A['b' + a][i] = b[a].values
    if r.sym in gf:
        ff = gf[r.sym]
        idx = np.ceil((ff.t.values - r.t0) / 3600000).astype(np.int64) - 1   # settled in (t, t+1h] -> bar index
        ok = (idx >= 0) & (idx < H)
        np.add.at(A['fund'][i], idx[ok], ff.rate.values[ok])
    # OKX: archive day (UTC+8) of the hour and of the hour 24h earlier both exist
    ts = pd.to_datetime(clock, unit='ms') + pd.Timedelta(hours=8)
    days = ts.normalize()
    if r.sym in cal.columns:
        c = cal[r.sym]
        on = c.reindex(days).fillna(False).values.astype(bool)
        on_prev = c.reindex(days - pd.Timedelta(days=1)).fillna(False).values.astype(bool)
        A['okx_on'][i] = on & on_prev
    # current OKX instruments: exact listTime also available; require both (conservative)
np.savez_compressed(os.path.join(D, 'panel.npz'), **A)
ev.to_parquet(os.path.join(D, 'events.parquet'))
print(ev.shape, 'okx-ever', int(A['okx_on'].any(1).sum()))
