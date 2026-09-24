import zipfile, io, numpy as np, pandas as pd
b = pd.read_csv(zipfile.ZipFile('dl/b_axs_2601.zip').open(zipfile.ZipFile('dl/b_axs_2601.zip').namelist()[0]))
o = pd.read_csv(zipfile.ZipFile('dl/o_axs_2601.zip').open(zipfile.ZipFile('dl/o_axs_2601.zip').namelist()[0]))
print(b.head(3)); print(o.head(3)); print(len(b), len(o))
b['t'] = pd.to_datetime((b.iloc[:, 0] / 3.6e6).round() * 3.6e6, unit='ms', utc=True)
o.columns = ['inst', 'rate', 'ft']; o['t'] = pd.to_datetime(o.ft, unit='ms', utc=True)
print('binance sum', b.iloc[:, 2].sum(), 'okx sum', o.rate.sum(), 'ann diff (okx-bin)', (o.rate.sum() - b.iloc[:, 2].sum()) * 365 / 31)
print('okx intervals (h):', o.t.diff().dt.total_seconds().div(3600).value_counts().head().to_dict())
print('bin intervals (h):', b.t.diff().dt.total_seconds().div(3600).value_counts().head().to_dict())
Z = np.load('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/panel.npz', allow_pickle=True)
coins = [str(c) for c in Z['coins']]; time = pd.to_datetime(Z['time'].astype('int64'), unit='ns', utc=True)
j = coins.index('AXS')
pf = pd.DataFrame({'fb': Z['fb'][:, j], 'fo': Z['fo'][:, j]}, index=time)['2026-01-01 01:00':'2026-02-01 00:00']
print('panel sums', pf.fb.sum(), pf.fo.sum())
ob = o.set_index('t').rate; bb = b.set_index('t').iloc[:, 2]
print('max abs diff okx vs panel', (pf.fo.reindex(ob.index) - ob).abs().max(), 'bin', (pf.fb.reindex(bb.index) - bb).abs().max())
# price path of AXS in Jan-Apr 2026
px = pd.DataFrame({'bc': Z['bc'][:, j], 'oc': Z['oc'][:, j], 'bmh': Z['bmh'][:, j], 'bml': Z['bml'][:, j], 'omh': Z['omh'][:, j], 'oml': Z['oml'][:, j]}, index=time)
d = px['2025-12-20':'2026-04-30'].resample('W').agg({'bc': 'last', 'oc': 'last', 'bmh': 'max', 'bml': 'min', 'omh': 'max', 'oml': 'min'})
print(d.round(3).to_string())
# 2022-11-13 AXS
w = px['2022-11-13 00:00':'2022-11-13 12:00']
print(w.round(3).to_string())
