"""Classify OKX USDT-swap listings against Binance UM perps: okx_only / okx_first (OKX >= 24h earlier) / bn_covered.
-> data/okx_events_a.csv"""
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import *
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.data.universe import base_asset, TRADFI, STABLE_OR_INDEX

sw = pd.read_csv(os.path.join(D, 'okx_swaps.csv'))
dk = pd.read_parquet(os.path.join(NL, 'daily_klines.parquet'), columns=['t', 'sym'])
dk = dk[dk.sym.str.endswith('USDT')]
first_day = dk.groupby('sym').t.min()
ev = pd.read_parquet(os.path.join(NL, 'events.parquet'))
t0_exact = dict(zip(ev.sym, ev.t0))
bn = {}
for s, t in first_day.items():
    b = base_asset(s)
    t = t0_exact.get(s, t)
    bn[b] = min(bn.get(b, t), t)
rows = []
for _, r in sw.iterrows():
    i = r.instId
    if not isinstance(i, str) or not i.isascii() or not i.endswith('-USDT-SWAP'):
        continue
    base = i[:-len('-USDT-SWAP')]
    first = int(r['first'])
    if first < pd.Timestamp('2022-01-03').value // 10 ** 6:
        continue
    cat = r.get('category')
    if r.get('live') in (True, 'True') and str(cat) not in ('1', '1.0'):
        continue                                     # current non-crypto (stocks, commodities, FX...)
    if base in TRADFI or base in STABLE_OR_INDEX:
        continue
    b = bn.get(base)
    if b is None:
        cls = 'okx_only'
    elif b <= first + 24 * 3600000:
        cls = 'bn_covered'
    else:
        cls = 'okx_first'
    rows.append(dict(instId=i, base=base, okx_first=first, bn_first=b, cls=cls, live=r.get('live'), src=r.src))
out = pd.DataFrame(rows)
out['year'] = pd.to_datetime(out.okx_first, unit='ms').dt.year
out.to_csv(os.path.join(D, 'okx_events_a.csv'), index=False)
print(pd.crosstab(out.year, out.cls, margins=True).to_string())
print(out[out.cls != 'bn_covered'].sort_values('okx_first').tail(30)[['instId', 'okx_first', 'cls', 'live', 'src']].assign(okx_first=lambda x: pd.to_datetime(x.okx_first, unit='ms')).to_string())
