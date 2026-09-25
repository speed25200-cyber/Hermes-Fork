"""Listing age as a cross-sectional factor on OKX-listed perps (daily bars, Binance archive incl. delisted coins).

Each rebalance (decided at the daily close, traded at that price, +lag days of latency): universe = Binance USDT perps
that OKX listed that day (point-in-time calendar from OKX trade archives), >= 2 days of Binance history, top-N by
trailing 7-day quote volume. Short the youngest fraction q (age = days since Binance perp listing, or 'token' age
= since the earlier of perp and Binance spot listing), long the oldest q (or long BTC), equal weight, 0.5 gross per
side (1x gross). Daily P&L = weights x close-to-close returns - weights x funding settled that day - costs on turnover
(fee 5 bp + slippage 10 bp if age < 30 days else 3 bp). Intraday worst case for drawdown: shorts at the day's high,
longs at the day's low, simultaneously. IS 2022-2024 selection by Sharpe; OOS 2025-01..2026-08.
-> results/agefactor_grid.csv, results/agefactor_eval.json"""
import itertools, json, os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')
OUT = os.path.join(BASE, 'results')
dk = pd.read_parquet(os.path.join(D, 'daily_klines.parquet'))
dk['d'] = pd.to_datetime(dk.t, unit='ms')
dk = dk[(dk.d >= '2021-11-01') & (dk.d <= '2026-08-31')]
first = pd.read_parquet(os.path.join(D, 'daily_klines.parquet')).groupby('sym').t.min()
first = pd.to_datetime(first, unit='ms')
C = dk.pivot(index='d', columns='sym', values='c')
Hh = dk.pivot(index='d', columns='sym', values='h').reindex_like(C)
Ll = dk.pivot(index='d', columns='sym', values='l').reindex_like(C)
QV = dk.pivot(index='d', columns='sym', values='qv').reindex_like(C)
fu = pd.read_parquet(os.path.join(D, 'funding_all.parquet'))
fu['d'] = pd.to_datetime(fu.t, unit='ms').dt.floor('D')
# funding settled at 00:00 of day d+1 belongs to holding day d -> shift by 1 ms back before flooring
fu['d'] = (pd.to_datetime(fu.t - 1, unit='ms')).dt.floor('D')
F = fu.groupby(['d', 'sym']).rate.sum().unstack().reindex(index=C.index, columns=C.columns).fillna(0.0)
cal = pd.read_parquet(os.path.join(D, 'okx_calendar.parquet'))
cal.index = cal.index.tz_convert(None)
OK = cal.reindex(index=C.index, columns=C.columns).fillna(False).astype(bool)
OK['USDCUSDT'] = False
sp = pd.read_csv(os.path.join(D, 'spot_first.csv'), parse_dates=['spot_first']).set_index('sym').spot_first
age_perp = pd.DataFrame({s: (C.index - first[s]).days for s in C.columns}, index=C.index).astype(float)
tok_first = pd.concat([first, sp.reindex(first.index)], axis=1).min(axis=1)
age_tok = pd.DataFrame({s: (C.index - tok_first[s]).days for s in C.columns}, index=C.index).astype(float)
R = C.pct_change(fill_method=None)
HR = Hh / C.shift(1) - 1          # worst intraday move vs previous close
LR = Ll / C.shift(1) - 1
QV7 = QV.rolling(7, min_periods=2).mean()
IS0, OOS0, END = '2022-01-01', '2025-01-01', '2026-08-31'


def weights(age_kind, q, N, reb, leg, exclude=None):
    age = age_perp if age_kind == 'perp' else age_tok
    elig = OK & C.notna() & (age_perp >= 2) & QV7.notna()
    if exclude:
        elig = elig.copy()
        elig[list(exclude)] = False
    W = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    last = None
    for i, d in enumerate(C.index):
        if reb == 'weekly' and d.dayofweek != 0 and last is not None:
            W.iloc[i] = last
            continue
        e = elig.loc[d]
        cand = QV7.loc[d][e].nlargest(N).index
        w = pd.Series(0.0, index=C.columns)
        if len(cand) >= 10:
            a = age.loc[d, cand].sort_values(kind='mergesort')
            k = max(1, int(round(q * len(a))))
            w[a.index[:k]] = -0.5 / k
            if leg == 'old':
                w[a.index[-k:]] += 0.5 / k
            else:
                w['BTCUSDT'] += 0.5
        W.iloc[i] = w.values
        last = w.values
    return W


def backtest(W, lag=0, cost_mult=1.0, L=1.0, start=IS0, end=END):
    Wt = W.shift(1 + lag).fillna(0.0) * L                 # decided at close d, held over day d+1
    young = (age_perp < 30)
    slip = np.where(young, 0.0010, 0.0003)
    turn = (W.shift(lag) * L).fillna(0.0).diff().abs().fillna(0.0)
    cost = (turn * (0.0005 + slip)).sum(axis=1) * cost_mult
    r = (Wt * R.fillna(0.0)).sum(axis=1) - (Wt * F).sum(axis=1) - cost.shift(1).fillna(0.0)
    worst = (Wt.clip(upper=0) * HR.fillna(0.0)).sum(axis=1) + (Wt.clip(lower=0) * LR.fillna(0.0)).sum(axis=1)
    r, worst = r[start:end], worst[start:end]
    gross = Wt.abs().sum(axis=1)[start:end]
    mmr = 0.05
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    liq = False
    rets = []
    for x, w, g in zip(r.values, worst.values, gross.values):
        ew = eq * (1 + min(w, x))
        mdd = max(mdd, 1 - ew / peak)
        if g > 0 and ew <= eq * g * mmr:
            liq = True
            rets.append(-1.0)
            break
        eq *= (1 + x)
        peak = max(peak, eq)
        rets.append(x)
    rr = pd.Series(rets + [0.0] * (len(r) - len(rets)), index=r.index)
    if liq:
        rr[len(rets):] = 0.0
    return rr, mdd, liq


def sharpe(r):
    s = r.std()
    return float(r.mean() / s * np.sqrt(365)) if s > 0 else 0.0


def yearly(r):
    return {int(y): float((1 + x).prod() - 1) for y, x in r.groupby(r.index.year)}


if __name__ == '__main__':
    rows = []
    cache = {}
    for age_kind, q, N, reb, leg in itertools.product(['perp', 'token'], [0.1, 0.2, 0.3], [50, 100, 200], ['weekly', 'daily'], ['old', 'btc']):
        W = weights(age_kind, q, N, reb, leg)
        cache[(age_kind, q, N, reb, leg)] = W
        a, mdda, la = backtest(W, start=IS0, end='2024-12-31')
        b, mddb, lb = backtest(W, start=OOS0, end=END)
        y = yearly(b)
        rows.append(dict(age=age_kind, q=q, N=N, reb=reb, leg=leg, is_sharpe=sharpe(a), is_ann=float(a.mean() * 365), is_maxdd=mdda,
                         oos_sharpe=sharpe(b), oos_ann=float(b.mean() * 365), oos_maxdd=mddb, y2025=y.get(2025), y2026=y.get(2026)))
        print(rows[-1], flush=True)
    G = pd.DataFrame(rows)
    G.to_csv(os.path.join(OUT, 'agefactor_grid.csv'), index=False)
    best = G.sort_values('is_sharpe', ascending=False).iloc[0]
    key = (best.age, best.q, int(best.N), best.reb, best.leg)
    W = cache[key]
    b, mdd, liq = backtest(W, start=OOS0, end=END)
    a, _, _ = backtest(W, start=IS0, end='2024-12-31')
    m = b.index.to_period('M')
    lomo = min(sharpe(b[m != p]) for p in m.unique())
    held = (W[OOS0:END] != 0).any()
    coins = [c for c in held[held].index if c != 'BTCUSDT']
    ev = dict(cfg=dict(age=best.age, q=float(best.q), N=int(best.N), reb=best.reb, leg=best.leg), is_sharpe=sharpe(a),
              oos_sharpe=sharpe(b), oos_years=yearly(b), oos_maxdd=mdd, oos_liq=liq, lomo_min=lomo,
              cost15_lat1=sharpe(backtest(W, lag=1, cost_mult=1.5, start=OOS0, end=END)[0]),
              half_kelly_is=float(0.5 * a.mean() / a.var()), n_coins_oos=len(coins))
    lev = {}
    for L in [1, 2, 3, 5, 8, 10, 15, 20]:
        r, dd, lq = backtest(W, L=L, start=OOS0, end=END)
        lev[L] = dict(sharpe=sharpe(r), maxdd=dd, liq=lq, ann=float(r.mean() * 365))
    ev['leverage'] = lev
    ok = [L for L, v in lev.items() if v['maxdd'] <= 0.35 and not v['liq'] and L <= ev['half_kelly_is']]
    ev['supportable_L'] = max(ok) if ok else 0
    if ev['oos_sharpe'] >= 1.0:          # leave-one-coin-out only worth its cost when the OOS Sharpe is competitive
        lc = []
        for c in coins:
            Wx = weights(best.age, best.q, int(best.N), best.reb, best.leg, exclude={c})
            lc.append(sharpe(backtest(Wx, start=OOS0, end=END)[0]))
        ev['loco_min'] = min(lc)
    json.dump(ev, open(os.path.join(OUT, 'agefactor_eval.json'), 'w'), indent=1, default=str)
    print(json.dumps(ev, indent=1, default=str))
