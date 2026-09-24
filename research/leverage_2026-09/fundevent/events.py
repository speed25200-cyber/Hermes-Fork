"""Build the settlement-event table from the Binance funding history.

For every (symbol, settlement time t):
  rate_t, interval_h(t), ann_t = rate_t * 8760 / interval_h   (realised at t: ORACLE, not known before t)
  rate_prev, ann_prev = the previous settlement of the same symbol (known at any time after t_prev: TRADABLE)
TradFi perps (equities, ETFs, metals, oil) are excluded; everything else, live or delisted, is kept.
Output: data/settle.parquet
"""
import json, os
import numpy as np, pandas as pd
from common import D

TRADFI_EXTRA = {'BABA', 'BITO', 'COPPER', 'DIS', 'DJT', 'EBAY', 'GDX', 'GS', 'HD', 'HK0700', 'HK1810', 'JPM',
                'KODEX200', 'KUAISHOU', 'MEITUAN', 'NATGAS', 'NVO', 'PANW', 'PAYP', 'PDD', 'SAMSUNGEM', 'SOFI',
                'TBT', 'TENCENT', 'TXN', 'TZA', 'UBER', 'XAU', 'XAG', 'XPD', 'XPT', 'CL', 'BZ', 'STXX', 'QNTX'}


def tradfi_set(f):
    inst = json.load(open(os.path.join(D, 'okx_instruments.json')))['data']
    nonc = {x['instId'].split('-')[0] for x in inst if x.get('instCategory') != '1'}
    first = f.groupby('sym').t.min()
    cut = pd.Timestamp('2025-12-01').value // 10 ** 6
    s = {sym for sym in first.index if (sym[:-4] in nonc and first[sym] >= cut) or sym[:-4] in TRADFI_EXTRA}
    return s


def build():
    f = pd.read_parquet(os.path.join(D, 'funding_all.parquet'))
    tf = tradfi_set(f)
    f = f[~f.sym.isin(tf)].copy()
    f = f.sort_values(['sym', 't']).reset_index(drop=True)
    f['ann'] = f.rate * 8760.0 / f.interval_h
    g = f.groupby('sym')
    f['t_prev'] = g.t.shift(1)
    f['rate_prev'] = g.rate.shift(1)
    f['ann_prev'] = g.ann.shift(1)
    f['ann_prev2'] = g.ann.shift(2)
    f['gap_h'] = (f.t - f.t_prev) / 3.6e6
    f.to_parquet(os.path.join(D, 'settle.parquet'))
    return f, tf


if __name__ == '__main__':
    f, tf = build()
    print('excluded TradFi', len(tf), 'kept symbols', f.sym.nunique(), 'settlements', len(f))
    f['yr'] = pd.to_datetime(f.t, unit='ms').dt.year
    for th in [0.5, 1.0, 2.0, 4.0]:
        m = f.ann_prev.abs() >= th
        print(f'|ann_prev|>={th:.0%}: events', int(m.sum()), 'pos', int((m & (f.ann_prev > 0)).sum()),
              'neg', int((m & (f.ann_prev < 0)).sum()), 'symbols', f.sym[m].nunique(),
              'sym-days', f[m].assign(d=f.t // 86400000).drop_duplicates(['sym', 'd']).shape[0])
        print('   by year', f[m].groupby('yr').size().to_dict())
    m = f.ann_prev.abs() >= 0.5
    x = f[m]
    print('persistence: corr(ann_t, ann_prev | extreme)', np.corrcoef(x.ann, x.ann_prev)[0, 1].round(3),
          ' same sign frac', (np.sign(x.ann) == np.sign(x.ann_prev)).mean().round(3),
          ' |ann_t|>=50% frac', (x.ann.abs() >= 0.5).mean().round(3))
    for th in [0.5, 1, 2, 4]:
        xx = f[f.ann_prev.abs() >= th]
        print(th, 'median ann_t/ann_prev', np.median(xx.ann / xx.ann_prev).round(3), 'mean |rate_t| bp', (xx.rate.abs().mean() * 1e4).round(2),
              'mean rate_t*sign(prev) bp', ((xx.rate * np.sign(xx.ann_prev)).mean() * 1e4).round(2), 'interval mix', xx.interval_h.value_counts().to_dict())
