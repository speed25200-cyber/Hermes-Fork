"""Leave-one-event-day-out, event-clustered bootstrap and stress=1 liquidations for the selected config
(btceth W1440 k80 x0 H480 both, taker lat0), K=1 portfolio, pm margin, per leverage.
Uses out/trades_sel_btceth_k80.csv (1m engine) and out/tick_verify_oos.csv (Binance ticks, touch 0.5 s)."""
import numpy as np, pandas as pd
import run_grid as R, engine as E
from common import T0

OUT = R.OUTDIR
LEVS = [1, 3, 5, 10, 15, 20]
OOS_Y = R.OOS_YEARS
tr = pd.read_csv(f'{OUT}/trades_sel_btceth_k80.csv', parse_dates=['t'])
tr = tr[(tr['exec'] == 'taker') & (tr['lat'] == 0)].sort_values(['t', 'coin'], kind='stable').reset_index(drop=True)
tr['t_entry'] = tr.t + pd.Timedelta(minutes=1)
tr['t_exit'] = tr.t_entry + pd.to_timedelta(tr.hold_min, unit='min')
keep, last = [], None
for i, r in tr.iterrows():
    if last is not None and r.t_entry <= last:
        continue
    keep.append(i); last = r.t_exit
k1 = tr.loc[keep].copy()
k1['day'] = k1.t.dt.strftime('%Y-%m-%d')
k1['seg'] = np.where(k1.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
print('K=1 trades IS', (k1.seg == 'IS').sum(), 'OOS', (k1.seg == 'OOS').sum())
print(k1[['coin', 't', 'side', 'net_bp', 'pm_ret5', 'pm_ret20', 'pm_worst20', 'seg']].round(4).to_string(index=False))

tv = pd.read_csv(f'{OUT}/tick_verify_oos.csv', parse_dates=['t'])
o = k1[k1.seg == 'OOS'].merge(tv[['coin', 't', 'net_touch_0.5', 'net_touch_2.0', 'net_w100k_0.5', 'net_touch_10.0']], on=['coin', 't'], how='left')


def cagr(rets):
    f = np.prod(1 + np.asarray(rets))
    return f ** (1 / OOS_Y) - 1 if f > 0 else -1.0


rows = []
for L in LEVS:
    base = o[f'pm_ret{L}'].values
    interest = L * o.net_bp.values / 1e4 - base
    variants = {'1m_model': base}
    for c in ['net_touch_0.5', 'net_touch_2.0', 'net_w100k_0.5', 'net_touch_10.0']:
        variants['tick_' + c[4:]] = L * o[c].values / 1e4 - interest
    for vn, rets in variants.items():
        d = dict(L=L, variant=vn, all=cagr(rets))
        for day in sorted(o.day.unique()):
            d['drop_' + day] = cagr(rets[o.day.values != day])
        d['drop_best_trade'] = cagr(np.delete(rets, np.argmax(rets)))
        rows.append(d)
res = pd.DataFrame(rows)
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 20)
print(res.round(4).to_string(index=False))
res.to_csv(f'{OUT}/loo_oos.csv', index=False, float_format='%.5g')

# event-clustered bootstrap of the mean net per trade (1x, all 26 per-coin trades and the K=1 list)
rng = np.random.default_rng(0)
for name, df in [('per-coin 26 trades', tr.assign(day=tr.t.dt.strftime('%Y-%m-%d'))), ('K=1', k1)]:
    days = df.day.unique()
    g = {d: df.net_bp[df.day == d].values for d in days}
    bs = []
    for _ in range(20000):
        pick = rng.choice(days, len(days), replace=True)
        v = np.concatenate([g[d] for d in pick]); bs.append(v.mean())
    bs = np.array(bs)
    print(name, 'n', len(df), 'days', len(days), 'mean %.1f bp' % df.net_bp.mean(),
          '95%% CI [%.1f, %.1f]' % tuple(np.percentile(bs, [2.5, 97.5])), 'P(mean<=0)=%.4f' % (bs <= 0).mean())

# stress=1 (premium-index intrabar excursion) and stress=0 liquidations per trade for the selected config
cfg = (1440, 80, 0.0, 480, 1, 0)
for st in [0, 1]:
    out = []
    for c in ['BTCUSDT', 'ETHUSDT']:
        P = R.prep(c)
        t = R.trades_for(P, c, cfg, 0, 0, stress=st)
        allt = t
        for r in t:
            out.append(dict(coin=c, t=T0 + pd.Timedelta(minutes=int(r[0])),
                            **{f'liq{int(L)}': r[E.col('pm', 'liq') + i] for i, L in enumerate(E.LEVS)},
                            **{f'worst{int(L)}': r[E.col('pm', 'worst') + i] for i, L in enumerate(E.LEVS)}))
        del P
    d = pd.DataFrame(out)
    d['seg'] = np.where(d.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
    print('stress', st, 'pm liquidations per coin-trade:', d.groupby('seg')[[f'liq{L}' for L in LEVS]].sum().to_dict('index'))
    print('stress', st, 'worst intrabar equity at 5x/10x/20x, OOS:', d[d.seg == 'OOS'][['coin', 't', 'worst5', 'worst10', 'worst20']].round(3).to_string(index=False))
