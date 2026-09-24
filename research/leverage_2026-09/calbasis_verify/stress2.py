import pandas as pd, numpy as np, multiprocessing as mp
from load import load
import cb_v as cb
AS = load()
B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
IS = ('2022-01-01', '2024-12-31 23:55'); OOS = ('2025-01-01', '2026-08-31 23:55'); FULL = ('2022-01-01', '2026-08-31 23:55')
def one(arg):
    tag, p, L, per, cost = arg
    sims, m, ec = cb.run_assets([AS['BTC'], AS['ETH']], p, L, 'cross', per[0], per[1], cost=cost)
    return dict(tag=tag, L=L, per='IS' if per == IS else 'OOS', cagr=round(m['cagr']*100, 2), dd=round(m['maxdd']*100, 1), liq=m['nliq'])
if __name__ == '__main__':
    args = []
    for L in [1, 10, 15, 20]:
        for per in (IS, OOS):
            args.append(('fill_worst_guard50bp', dict(B, fill='worst', mark_guard=0.005), L, per, cb.COST))
            args.append(('fill_worst_guard50bp+slip_x2', dict(B, fill='worst', mark_guard=0.005), L, per, dict(cb.COST, slip_perp=0.0002, slip_fut=0.0006)))
    with mp.Pool(4) as pool:
        res = pool.map(one, args)
    df = pd.DataFrame(res)
    print(df.pivot_table(index=['tag', 'L'], columns='per', values=['cagr', 'dd', 'liq'], sort=False).to_string())
    # FULL continuous run, OOS segment (positions carried from 2024, all parameters frozen)
    rows = []
    for L in [1, 3, 5, 10, 15, 20]:
        sims, m, ec = cb.run_assets([AS['BTC'], AS['ETH']], B, L, 'cross', *FULL)
        ecf, elf = cb.equity_series(sims, [.5, .5], *FULL)
        seg = ecf[OOS[0]:OOS[1]]; segl = elf[OOS[0]:OOS[1]]
        # start the segment at the last value of 2024
        e0 = ecf[:'2024-12-31 23:55'].iloc[-1]
        seg = pd.concat([pd.Series([e0], index=[pd.Timestamp('2024-12-31 23:55')]), seg]); segl = pd.concat([pd.Series([e0], index=[pd.Timestamp('2024-12-31 23:55')]), segl])
        mm = cb.metrics_from(seg, segl)
        liq_oos = sum(1 for s in sims for t in s.trades if t['why'] == 'LIQ' and t['exit'] >= '2025')
        rows.append(dict(L=L, oos_seg_cagr=round(mm['cagr']*100, 2), oos_seg_dd=round(mm['maxdd']*100, 1), oos_seg_sharpe=round(mm['sharpe'], 2),
                         oos_seg_worstday=round(mm['worst_day']*100, 1), liq=liq_oos, **{k: round(v*100, 1) for k, v in mm['years'].items()}))
    d2 = pd.DataFrame(rows); print(d2.to_string()); d2.to_csv('fullrun_oos_segment.csv', index=False)
    df.to_csv('stress_fill_guard.csv', index=False)
