"""Equal-weight hourly alt index (large alts from the xvenue Binance 1h cache, BTC excluded), same construction as
newlisting/althedge.py. Caveat: the constituent list is today's large alts (survivorship: tilts index return up).
-> altidx_h.parquet (t ms index: o,h,l,c,n), altidx_d.parquet (daily close, 30d return known at day start)."""
import glob, os
import numpy as np, pandas as pd
G0 = int(pd.Timestamp('2021-12-01').value // 10 ** 6); HMS = 3600000
X = {}
for f in glob.glob('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/binance/*_klines.parquet'):
    c = os.path.basename(f).split('_')[0]
    if c == 'BTC':
        continue
    d = pd.read_parquet(f)
    d['t'] = d.open_time.astype('int64')
    X[c] = d.set_index('t')[['open', 'high', 'low', 'close']]
print(len(X), sorted(X))
idx = np.arange(G0, int(pd.Timestamp('2026-09-01').value // 10 ** 6) + 200 * 86400000, HMS, dtype=np.int64)
cl = pd.DataFrame({c: v.close for c, v in X.items()}).reindex(idx)
hi = pd.DataFrame({c: v.high for c, v in X.items()}).reindex(idx)
lo = pd.DataFrame({c: v.low for c, v in X.items()}).reindex(idx)
op = pd.DataFrame({c: v.open for c, v in X.items()}).reindex(idx)
pc = cl.shift(1)
r = (cl / pc - 1).mean(axis=1).fillna(0.0)
I = (1 + r).cumprod()
Ih = (I.shift(1) * (hi / pc).mean(axis=1)).fillna(I)
Il = (I.shift(1) * (lo / pc).mean(axis=1)).fillna(I)
Io = (I.shift(1) * (op / pc).mean(axis=1)).fillna(I)
n = cl.notna().sum(axis=1)
out = pd.DataFrame({'o': Io, 'h': Ih, 'l': Il, 'c': I, 'n': n}, index=idx)
out.index.name = 't'
out.to_parquet('altidx_h.parquet')
ts = pd.to_datetime(idx, unit='ms')
dc = pd.Series(I.values, ts).resample('D').last()
dd = pd.DataFrame({'close': dc})
dd['ret30_known'] = (dc / dc.shift(30) - 1).shift(1)     # known at the start of day D (uses closes up to D-1)
dd['n'] = pd.Series(n.values, ts).resample('D').last()
dd.to_parquet('altidx_d.parquet')
print(dd.dropna().describe())
print(dd.loc['2022-01-01':'2026-09-01'].resample('YE').close.last().pct_change())
