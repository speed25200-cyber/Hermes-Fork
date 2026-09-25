"""Event grid: every (side, d0, d1, beta, stop, universe) simulated at 1x on IS (entries 2022-01..2024-12) and OOS
(entries 2025-01..2026-08) separately. Selection rule, fixed before looking at OOS: highest IS Sharpe among configs
with >= 30 IS trades and no IS liquidation. -> results/grid.csv"""
import itertools, json, os, sys
from multiprocessing import Pool
from sim import *

OUT = os.path.join(BASE, 'results')
os.makedirs(OUT, exist_ok=True)
D0 = [1, 6, 24, 72, 168, 336, 720]
D1 = [3, 7, 14, 30, 60, 90]
CFGS = []
for side, d0, d1, beta, stop, uni in itertools.product([-1, 1], D0, D1, [0.0, 1.0], [None, 0.25, 0.5, 1.0], ['okx', 'newtok']):
    if d1 * 24 < d0 + 24:
        continue
    if side > 0 and stop == 1.0:
        continue
    CFGS.append(dict(side=side, d0=d0, d1=d1 * 24, beta=beta, stop=stop, uni=uni, K=5))
PRICE = sys.argv[1] if len(sys.argv) > 1 else 'hybrid'     # 'hybrid' (OKX candles where served) or 'binance'
_D = None


def run(cfg):
    global _D
    if _D is None:
        _D = Data(PRICE)
    a = summarize(simulate(_D, cfg, IS_START, OOS_START))
    b = summarize(simulate(_D, cfg, OOS_START, OOS_END))
    row = dict(cfg)
    row['d1'] = cfg['d1'] // 24
    row['stop'] = cfg['stop'] if cfg['stop'] is not None else 0.0
    for k, v in (('is', a), ('oos', b)):
        row.update({f'{k}_sharpe': v['sharpe'], f'{k}_cagr': v['cagr'], f'{k}_maxdd': v['maxdd'], f'{k}_liq': v['liq'],
                    f'{k}_trades': v['trades'], f'{k}_vol': v['vol']})
        for y, r in v['years'].items():
            row[f'y{y}'] = r
    return row


if __name__ == '__main__':
    print(len(CFGS), 'configs', flush=True)
    with Pool(4) as p:
        rows = p.map(run, CFGS, chunksize=8)
    g = pd.DataFrame(rows)
    g['price'] = PRICE
    g.to_csv(os.path.join(OUT, f'grid_{PRICE}.csv'), index=False)
    print('saved', g.shape)
