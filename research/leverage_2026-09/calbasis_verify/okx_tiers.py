"""Re-run short_B with OKX tier-1 maintenance margin rates taken from the live OKX position-tiers API (2026-09-24):
BTC-USD inverse futures 0.65%, ETH-USD inverse futures 2.0%, BTC/ETH-USD_UM futures 2.0%, BTC/ETH-USDT perps 0.4%.
Per-asset cost dicts; portfolio = 50/50 BTC/ETH sub-accounts (as in the original)."""
import pandas as pd, numpy as np, multiprocessing as mp
from load import load
import cb_v as cb
AS = load()
B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
PER = {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}
SCEN = {
    'orig_0.5%': {'BTC': dict(cb.COST), 'ETH': dict(cb.COST)},
    'okx_inverse_tiers': {'BTC': dict(cb.COST, mmr_fut=0.0065), 'ETH': dict(cb.COST, mmr_fut=0.02)},
    'okx_UM_tiers': {'BTC': dict(cb.COST, mmr_fut=0.02), 'ETH': dict(cb.COST, mmr_fut=0.02)},
}
def one(arg):
    sc, L, per = arg
    a, b = PER[per]
    sims = [cb.Sim(AS[x], B, L, 'cross', SCEN[sc][x], a, b).run() for x in ['BTC', 'ETH']]
    ec, el = cb.equity_series(sims, [.5, .5], a, b)
    m = cb.metrics_from(ec, el)
    r = dict(scen=sc, L=L, per=per, cagr=round(m['cagr']*100, 2), dd=round(m['maxdd']*100, 1), sharpe=round(m['sharpe'], 2),
             worst_day=round(m['worst_day']*100, 1), liq=sum(s.nliq for s in sims),
             liq_detail=';'.join(f"{t['con']}@{t['exit']}" for s in sims for t in s.trades if t['why'] == 'LIQ'),
             trades=sum(s.nentry for s in sims), **{'y' + k: round(v*100, 1) for k, v in m['years'].items()})
    if per == 'FULL':
        e0 = ec[:'2024-12-31 23:55'].iloc[-1]
        seg = ec['2025-01-01':]; segl = el['2025-01-01':]
        seg = pd.concat([pd.Series([e0], index=[pd.Timestamp('2024-12-31 23:55')]), seg]); segl = pd.concat([pd.Series([e0], index=[pd.Timestamp('2024-12-31 23:55')]), segl])
        mm = cb.metrics_from(seg, segl)
        r.update(oos_seg_cagr=round(mm['cagr']*100, 2), oos_seg_dd=round(mm['maxdd']*100, 1))
    return r
if __name__ == '__main__':
    args = [(sc, L, per) for sc in SCEN for L in [1, 3, 5, 10, 15, 20] for per in PER]
    with mp.Pool(4) as pool:
        res = pool.map(one, args)
    df = pd.DataFrame(res); pd.set_option('display.width', 300)
    print(df.drop(columns=['liq_detail']).to_string())
    print(df[df.liq > 0][['scen', 'L', 'per', 'liq_detail']].to_string())
    df.to_csv('okx_tiers_results.csv', index=False)
