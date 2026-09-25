"""Check of the intrabar proxy on 2025-10-10 (worst day): TSMOM unit weights at the 2025-10-09 close, adverse
excursion from (a) the pre-registered proxy (Binance daily extremes, all simultaneous) and (b) OKX hourly MARK prices
for coins cached in ../xvenue (38 coins), same hour for all; other coins keep the proxy. Then the OOS 1x intrabar max
drawdown with (b) substituted on that day."""
import numpy as np, pandas as pd, json
import tsmom as M
SP = M.SP
P = M.load(); cache = {}
Wt, mem, rank = M.target_weights(dict(N=30, lbs=[10, 20, 40], kind='z', hl=60), cache)
T = M.backtest(Wt, rank, band=0.5)
D = P['dates']; S = np.array(P['syms'])
t = D.searchsorted(pd.Timestamp('2025-10-09', tz='UTC'))
# the band means holdings differ slightly from targets: rebuild holdings by re-running the loop up to t
w = np.zeros(len(S)); r = np.nan_to_num(P['r'])
t0 = D.searchsorted(pd.Timestamp(M.IS_START, tz='UTC'))
for k in range(t0 - 1, t + 1):
    tgt = Wt[k]
    na = max((np.abs(tgt) > 0).sum(), 1); thr = 0.5 * np.abs(tgt).sum() / na
    trade = (np.abs(tgt - w) > thr) | ((tgt == 0) & (w != 0))
    w = np.where(trade, tgt, w)
    if k < t:
        ret = np.sum(w * (r[k + 1] - P['F'][k + 1]))
        w = w * (1 + r[k + 1]) / (1 + ret)
proxy = np.minimum(w * P['lo'][t + 1], w * P['hi'][t + 1])
hourly = None; used = []
for j in np.where(w != 0)[0]:
    base = S[j][:-4]
    try:
        d = pd.read_parquet(f'{SP}/xvenue/data/okx/{base}_mark.parquet')
    except Exception:
        continue
    d['t'] = pd.to_datetime(d.ts, unit='ms', utc=True); d = d.set_index('t').sort_index()
    prev = d.close.loc[:'2025-10-09 23:00'].iloc[-1]; day = d.loc['2025-10-10 00:00':'2025-10-10 23:00']
    adv = (day.low / prev - 1) if w[j] > 0 else (day.high / prev - 1)
    hourly = w[j] * adv if hourly is None else hourly.add(w[j] * adv, fill_value=0)
    used.append(j)
used = np.array(used)
others = np.setdiff1d(np.where(w != 0)[0], used)
ib_b = hourly.min() + proxy[others].sum()
out = dict(gross=float(np.abs(w).sum()), n_okx_hourly=len(used), proxy_all=float(proxy.sum()),
           proxy_same_coins=float(proxy[used].sum()), okx_hourly_mark_same_coins=float(hourly.min()),
           worst_hour=str(hourly.idxmin()), ib_with_okx_hourly=float(ib_b))
T2 = T.copy()
T2.loc['2025-10-10', 'ib'] = min(ib_b - T.loc['2025-10-10', 'cost'], T.loc['2025-10-10', 'ret'])
for name, X in [('proxy', T), ('okx_hourly_on_1010', T2)]:
    o = X.loc[M.OOS_START:M.OOS_END]
    E, pk, mdd = 1.0, 1.0, 0.0
    for rr, ii in zip(o.ret.values, o.ib.values):
        mdd = max(mdd, 1 - E * (1 + ii) / pk); E *= 1 + rr; pk = max(pk, E); mdd = max(mdd, 1 - E / pk)
    out[f'maxdd_1x_{name}'] = mdd
print(json.dumps(out, indent=1))
json.dump(out, open(f'{M.W}/check_1010.json', 'w'), indent=1)
