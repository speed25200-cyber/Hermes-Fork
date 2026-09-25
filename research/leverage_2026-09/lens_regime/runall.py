"""Daily returns (1x, researcher's sim, hybrid OKX prices) for every grid config, IS (2022-01..2024-12) and OOS
(2025-01..2026-08) as separate runs (as grid.py), under hedge = 'btc' (grid beta as is: 0 or 1 BTC) and, for beta=1
configs, hedge = 'alt' (BTC leg replaced by the EW alt index). -> daily_all.parquet (columns = config keys),
trades_plateau.parquet (trades of the day-1/3 -> day-7 short configs)"""
import sys, itertools, pickle
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
from multiprocessing import Pool
D0 = [1, 6, 24, 72, 168, 336, 720]
D1 = [3, 7, 14, 30, 60, 90]
CFGS = []
for side, d0, d1, beta, stop, uni in itertools.product([-1, 1], D0, D1, [0.0, 1.0], [None, 0.25, 0.5, 1.0], ['okx', 'newtok']):
    if d1 * 24 < d0 + 24:
        continue
    if side > 0 and stop == 1.0:
        continue
    CFGS.append(dict(side=side, d0=d0, d1=d1 * 24, beta=beta, stop=stop, uni=uni, K=5))
JOBS = [(c, 'btc') for c in CFGS] + [(c, 'alt') for c in CFGS if c['beta'] == 1.0]
_D = {}

def key(c, h):
    return f"s{c['side']}_d{c['d0']}_e{c['d1']//24}_b{int(c['beta'])}_st{c['stop'] or 0}_{c['uni']}_{h}"

def data(h):
    if h not in _D:
        Dt = Data('hybrid')
        if h == 'alt':
            A = pd.read_parquet('altidx_h.parquet')
            gi = ((Dt.ev.t0.values - G0) // HMS).astype(np.int64)
            k = np.clip(gi[:, None] + np.arange(Dt.H)[None, :], 0, len(A) - 1)
            Dt.bo, Dt.bh, Dt.bl, Dt.bc = (A[x].values[k] for x in 'ohlc')
        _D[h] = Dt
    return _D[h]

def run(job):
    c, h = job
    Dt = data(h)
    a = simulate(Dt, c, IS_START, OOS_START)
    b = simulate(Dt, c, OOS_START, OOS_END)
    r = pd.concat([a['ret'], b['ret']])
    tr = None
    if c['side'] == -1 and c['d0'] in (24, 72) and c['d1'] == 168:
        tr = pd.DataFrame(a['trades'] + b['trades'])
        tr['key'] = key(c, h)
    return key(c, h), r.astype('float32'), (a['liq'], b['liq']), tr

if __name__ == '__main__':
    with Pool(4) as p:
        out = p.map(run, JOBS, chunksize=4)
    R = pd.DataFrame({k: r for k, r, _, _ in out})
    R.to_parquet('daily_all.parquet')
    meta = pd.DataFrame([dict(key=k, liq_is=l[0], liq_oos=l[1]) for k, _, l, _ in out])
    meta.to_csv('liq_all.csv', index=False)
    T = pd.concat([t for _, _, _, t in out if t is not None], ignore_index=True)
    T.drop(columns=['E_before']).to_parquet('trades_plateau.parquet')
    print(R.shape, T.shape)
