import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify')
import carry_sim_v as cv, pandas as pd
from multiprocessing import Pool
bands = {1: 0.2, 3: 0.2, 5: 0.2, 8: 0.2, 10: 0.2, 15: 0.2, 20: 0.05}
PER = {'IS': ('2022-01-01', '2024-12-31 23:00'), 'OOS': ('2025-01-01', '2026-08-31 23:00'), 'FULL': ('2022-01-01', '2026-08-31 23:00')}
def run(a):
    L, per, ib = a
    r = cv.sim(['BTCUSDT', 'ETHUSDT'], L, band=bands[L], start=PER[per][0], end=PER[per][1], intrabar=ib)
    y = r.pop('years')
    return dict(L=L, per=per, intrabar=ib, cagr=round(r['cagr'], 4), maxdd=round(r['maxdd'], 4), worst_day=round(r['worst_day'], 4), sharpe=round(r['sharpe'], 2),
                liq=r['liquidations'], liq_dates=r['liq_dates'], trades=r['trades'], m1_hours=r['m1_used'], **{f'y{k}': round(v, 4) for k, v in y.items()})
if __name__ == '__main__':
    jobs = [(L, per, ib) for ib in ['proxy', '1m'] for L in [1, 3, 5, 8, 10, 15, 20] for per in ['IS', 'OOS', 'FULL']]
    with Pool(4) as p:
        res = p.map(run, jobs, chunksize=1)
    df = pd.DataFrame(res)
    df.to_csv('m1_vs_proxy.csv', index=False)
    pd.set_option('display.width', 250)
    print(df.drop(columns=[c for c in df.columns if c.startswith('y')]).to_string())
    print(df[df.per == 'FULL'][['L', 'intrabar'] + [c for c in df.columns if c.startswith('y')]].to_string())
