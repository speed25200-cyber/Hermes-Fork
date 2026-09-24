"""Diagnostic only (NOT selection): OOS result at 10/15/20x for every band in the IS grid, 1m intrabar check, taker and maker fees."""
import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify')
import carry_sim_v as cv, pandas as pd
from multiprocessing import Pool
def run(a):
    L, b, mk = a
    r = cv.sim(['BTCUSDT', 'ETHUSDT'], L, band=b, start='2025-01-01', end='2026-08-31 23:00', intrabar='1m', maker=mk)
    return dict(L=L, band=b, maker=mk, cagr_oos=round(r['cagr'], 4), maxdd_oos=round(r['maxdd'], 4), liq=r['liquidations'], liq_dates=r['liq_dates'], trades=r['trades'], fees=round(r['fees'], 3), funding=round(r['funding'], 3), interest=round(r['interest'], 3))
if __name__ == '__main__':
    jobs = [(L, b, mk) for L in [1, 10, 15, 20] for b in [0.02, 0.05, 0.1, 0.2] for mk in [False, True]]
    with Pool(4) as p:
        df = pd.DataFrame(p.map(run, jobs))
    df.to_csv('band_sensitivity_oos.csv', index=False)
    print(df.to_string())
