import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry')
import carry_sim as cs, pandas as pd
o = pd.read_csv(cs.D + '/okx_funding_recent.csv', parse_dates=['ts'])
P = cs.load()
for c in ['BTC', 'ETH']:
    oc = o[o.coin == c].set_index('ts').rate.sort_index()
    a, b = oc.index.min(), pd.Timestamp('2026-08-31 23:59', tz='UTC')
    oc = oc[a:b]
    bn = P['fund'][c + 'USDT'][a - pd.Timedelta(hours=1):b]
    days = (b - a).total_seconds() / 86400
    print(c, a.date(), '..', b.date(), f'OKX {oc.sum()*365/days*100:.2f}%  Binance {bn.sum()*365/days*100:.2f}%  n_okx={len(oc)} n_bn={(bn!=0).sum()}')
