"""Diagnostic (not used for selection): is the new-listing short an idiosyncratic effect or alt beta?
Replace the BTC hedge leg with an equal-weight hourly index of 37 large alts (xvenue cache: Binance 1h klines of
ETH, SOL, XRP, DOGE, ... 2022-01..2026-08; alts listed later enter when they start). Index high/low = index close
x mean(h/prev close) / mean(l/prev close) across coins. Runs the top IS-ranked configs (hybrid prices) with beta 1.
-> results/althedge.csv"""
import glob
from sim import *
X = {}
for f in glob.glob('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/binance/*_klines.parquet'):
    c = os.path.basename(f).split('_')[0]
    if c == 'BTC':
        continue
    d = pd.read_parquet(f)
    d['t'] = d.open_time.astype('int64')
    X[c] = d.set_index('t')[['open', 'high', 'low', 'close']]
idx = np.arange(G0, int(pd.Timestamp('2026-09-01').value // 10 ** 6) + 200 * 86400000, HMS, dtype=np.int64)
cl = pd.DataFrame({c: v.close for c, v in X.items()}).reindex(idx)
hi = pd.DataFrame({c: v.high for c, v in X.items()}).reindex(idx)
lo = pd.DataFrame({c: v.low for c, v in X.items()}).reindex(idx)
op = pd.DataFrame({c: v.open for c, v in X.items()}).reindex(idx)
pc = cl.shift(1)
r = (cl / pc - 1).mean(axis=1).fillna(0.0)
I = (1 + r).cumprod()
Ih = I.shift(1) * (hi / pc).mean(axis=1)
Il = I.shift(1) * (lo / pc).mean(axis=1)
Io = I.shift(1) * (op / pc).mean(axis=1)
Ih, Il, Io = Ih.fillna(I), Il.fillna(I), Io.fillna(I)
Dt = Data('hybrid')
gi = ((Dt.ev.t0.values - G0) // HMS).astype(np.int64)
k = gi[:, None] + np.arange(Dt.H)[None, :]
k = np.clip(k, 0, len(idx) - 1)
Dt.bo, Dt.bh, Dt.bl, Dt.bc = Io.values[k], Ih.values[k], Il.values[k], I.values[k]
g = pd.read_csv(os.path.join(BASE, 'results', 'grid_hybrid.csv'))
el = g[(g.is_trades >= 30) & (~g.is_liq) & (g.beta == 1.0)].sort_values('is_sharpe', ascending=False).head(8)
rows = []
for _, row in el.iterrows():
    cfg = dict(side=int(row.side), d0=int(row.d0), d1=int(row.d1) * 24, beta=1.0, stop=None if row.stop == 0 else float(row.stop), uni=row.uni, K=5)
    a = summarize(simulate(Dt, cfg, IS_START, OOS_START))
    b = summarize(simulate(Dt, cfg, OOS_START, OOS_END))
    rows.append(dict(d0=cfg['d0'], d1=row.d1, stop=row.stop, uni=row.uni, btc_is=row.is_sharpe, btc_oos=row.oos_sharpe,
                     alt_is=a['sharpe'], alt_oos=b['sharpe'], alt_y2025=b['years'].get(2025), alt_y2026=b['years'].get(2026), alt_oos_maxdd=b['maxdd']))
    print(rows[-1], flush=True)
pd.DataFrame(rows).to_csv(os.path.join(BASE, 'results', 'althedge.csv'), index=False)
