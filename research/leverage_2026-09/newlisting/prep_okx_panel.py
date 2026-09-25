"""OKX 1H candles on each event's clock, in Binance price units (x1000 etc.). NaN where OKX has no candle.
-> data/panel_okx.npz (o, h, l, c)"""
import sys
sys.path.insert(0, '/home/user/Hermes/src')
from common import *
from hermes.execution.okx.instruments import binance_price_factor
ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
P = np.load(os.path.join(D, 'panel2.npz'))
n, H = P['o'].shape
ok = pd.read_parquet(os.path.join(D, 'okx_h1.parquet'))
g = dict(tuple(ok.groupby('sym')))
A = {k: np.full((n, H), np.nan) for k in 'ohlc'}
for i, r in ev.iterrows():
    if r.sym not in g:
        continue
    f = binance_price_factor(r.sym)
    clock = r.t0 + np.arange(H, dtype=np.int64) * 3600000
    k = g[r.sym].set_index('t').reindex(clock)
    for a in 'ohlc':
        A[a][i] = k[a].values * f
np.savez_compressed(os.path.join(D, 'panel_okx.npz'), **A)
m = np.isfinite(A['c']) & np.isfinite(P['c'])
d = np.abs(np.log(A['c'][m] / P['c'][m]))
print('events with OKX candles', int(np.isfinite(A['c']).any(1).sum()), '| |log(okx/binance close)| median %.5f p99 %.4f max %.3f' % (np.median(d), np.quantile(d, .99), d.max()))
