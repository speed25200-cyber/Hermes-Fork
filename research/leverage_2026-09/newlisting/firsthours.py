"""First hours after the OKX perp listing, on OKX's own 1H candles (contracts still listed on OKX only: delisted
ones are not served by the REST API -> survivorship; for shorts this bias is against the strategy). Event = first OKX
candle, if it falls after Binance listing - 3 days (so it is the OKX listing, not the start of our download).
Trade: short (or long) at the open of listing hour + h0, cover at the open of + h1; stop none; costs 5 bp fee +
30 bp slippage per side when h0 < 6 else 10 bp; intrabar max adverse excursion reported. Per-trade stats IS/OOS and a
portfolio Sharpe (each trade 20% of equity, daily P&L summed, idle days 0).
-> results/firsthours.csv"""
import itertools
from sim import *
ok = pd.read_parquet(os.path.join(D, 'okx_h1.parquet'))
ev = pd.read_parquet(os.path.join(D, 'events.parquet')).set_index('sym')
rows = []
for sym, g in ok.groupby('sym'):
    g = g.sort_values('t').set_index('t')
    t_ok = g.index[0]
    if t_ok <= ev.loc[sym, 't0'] - 3 * 86400000 + 3600000:
        continue                       # OKX listed before our window: listing hour unknown
    rows.append((sym, t_ok, g))
print(len(rows), 'OKX listings observed', flush=True)
out = []
for side, h0, h1 in itertools.product([-1, 1], [1, 2, 4, 6, 12, 24], [4, 12, 24, 48, 72]):
    if h1 <= h0:
        continue
    slip = 0.0030 if h0 < 6 else 0.0010
    tr = []
    for sym, t_ok, g in rows:
        a, b = t_ok + h0 * HMS, t_ok + h1 * HMS
        if a not in g.index or b not in g.index:
            continue
        pa, pb = g.loc[a, 'o'], g.loc[b, 'o']
        seg = g.loc[a:b - HMS]
        mae = (seg.h.max() / pa - 1) if side < 0 else (1 - seg.l.min() / pa)
        r = side * (pb / pa - 1) - 2 * (0.0005 + slip)
        tr.append((pd.Timestamp(a, unit='ms'), r, mae))
    T = pd.DataFrame(tr, columns=['t', 'r', 'mae'])
    for per, m in (('IS', T.t < '2025-01-01'), ('OOS', T.t >= '2025-01-01')):
        x = T[m & (T.t >= '2022-01-01')]
        if len(x) < 3:
            continue
        daily = x.groupby(x.t.dt.floor('D')).r.sum() * 0.2
        span = pd.date_range('2022-01-01' if per == 'IS' else '2025-01-01', '2024-12-31' if per == 'IS' else '2026-08-31')
        dr = daily.reindex(span).fillna(0.0)
        out.append(dict(side=side, h0=h0, h1=h1, per=per, n=len(x), mean=x.r.mean(), median=x.r.median(),
                        t=x.r.mean() / x.r.std() * np.sqrt(len(x)), hit=(x.r > 0).mean(), mae_p90=x.mae.quantile(0.9),
                        mae_max=x.mae.max(), port_sharpe=sharpe(dr)))
R = pd.DataFrame(out)
R.to_csv(os.path.join(BASE, 'results', 'firsthours.csv'), index=False)
W = R.pivot_table(index=['side', 'h0', 'h1'], columns='per', values=['n', 'mean', 'port_sharpe', 'mae_p90'])
pd.set_option('display.width', 250)
print(W.round(3).to_string())
