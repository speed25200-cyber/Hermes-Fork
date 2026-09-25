"""Hourly intrabar bounds for the book (its own hourly path is not saved), per unit of BOOK GROSS, from the Binance 1h
bars of its point-in-time universe (30 names). For day d, hour h (UTC), with o_i the 00:00 open of name i:
  lo_i(h) = low_i(h)/o_i - 1, hi_i(h) = high_i(h)/o_i - 1   (hour-h extremes relative to the day open)
  k = number of positions per side = round(n_positions_d / 2) (actual daily breadth, 2..15)
  A (adversarial, opposite extremes): 0.5*mean(k lowest lo) - 0.5*mean(k highest hi)   [upper bound]
  B (adversarial, synchronous):      min over s in {lo, hi} of 0.5*(mean(k lowest s) - mean(k highest s))
  C (random book p99, synchronous):  1st percentile over 4000 random k/k books of min over s of 0.5*(mean_long s - mean_short s)
  M (market-basket range):           0.5*(mean lo) - 0.5*(mean hi)  (same basket both legs, opposite extremes)
Also the equal-weight basket low/high (for the net exposure term). -> book_paths.parquet (day, hour, A, B, C, M, mlo, mhi)"""
import numpy as np, pandas as pd
rng = np.random.default_rng(7)
U = pd.read_parquet('universe_daily.parquet')
U.index = U.index.tz_localize(None)
h1 = pd.read_parquet('h1_universe.parquet')
h1['ts'] = pd.to_datetime(h1.t, unit='ms')
B = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv', index_col=0, parse_dates=True)
B.index = B.index.tz_localize(None)
H = {c: h1.pivot_table(index='ts', columns='sym', values=c) for c in ('o', 'h', 'l')}
days = pd.date_range('2025-01-01', '2026-08-31', freq='D')
rows = []
NR = 4000
for d in days:
    mem = [s for s in U.columns[U.loc[d].values] if s in H['o'].columns]
    hrs = pd.date_range(d, periods=24, freq='h')
    o = H['o'].reindex(index=hrs, columns=mem)
    hi = H['h'].reindex(index=hrs, columns=mem)
    lo = H['l'].reindex(index=hrs, columns=mem)
    o0 = o.iloc[0]
    ok = o0.notna() & hi.notna().all() & lo.notna().all()
    o0, hi, lo = o0[ok], hi.loc[:, ok], lo.loc[:, ok]
    n = int(ok.sum())
    npos = B.n_positions.get(d, 26.0)
    k = int(np.clip(round(npos / 2), 2, n // 2))
    LO = (lo.values / o0.values - 1)     # 24 x n
    HI = (hi.values / o0.values - 1)
    sL, sH = np.sort(LO, 1), np.sort(HI, 1)
    A = 0.5 * sL[:, :k].mean(1) - 0.5 * sH[:, -k:].mean(1)
    Bs = np.minimum(0.5 * (sL[:, :k].mean(1) - sL[:, -k:].mean(1)), 0.5 * (sH[:, :k].mean(1) - sH[:, -k:].mean(1)))
    # random k/k books
    perm = np.argsort(rng.random((NR, n)), 1)
    longs, shorts = perm[:, :k], perm[:, k:2 * k]
    def rb(X):
        return 0.5 * (X[:, longs].mean(2) - X[:, shorts].mean(2))    # 24 x NR
    C = np.quantile(np.minimum(rb(LO), rb(HI)), 0.01, axis=1)
    M = 0.5 * LO.mean(1) - 0.5 * HI.mean(1)
    for hh in range(24):
        rows.append(dict(day=d, hour=hh, A=A[hh], B=Bs[hh], C=C[hh], M=M[hh], mlo=LO[hh].mean(), mhi=HI[hh].mean(), n=n, k=k))
P = pd.DataFrame(rows)
P.to_parquet('book_paths.parquet')
dm = P.groupby('day')[['A', 'B', 'C', 'M', 'mlo', 'mhi']].min()
print('per-day worst hour (per unit book gross), quantiles over OOS days:')
print(dm.quantile([0.5, 0.9, 0.99]).round(4).to_string())
print('worst days by B:'); print(dm.nsmallest(8, 'B').round(4).to_string())
for d in ('2025-10-10', '2025-02-03', '2025-04-07', '2025-03-03'):
    x = P[P.day == d].set_index('hour')
    w = x.B.idxmin()
    print(d, 'worst hour (B)', w, x.loc[w, ['A', 'B', 'C', 'M', 'mlo', 'mhi']].round(4).to_dict(), 'n', x.n.iloc[0], 'k', x.k.iloc[0])
