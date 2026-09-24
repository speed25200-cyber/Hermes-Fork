"""Main runner: event reversal (wick), grid, opening-range breakout on 7 USDT perps, 1m bars, L in {1,3,5,10,15,20}.

Pre-registered grids (fixed in prereg.json before any return was computed):
  WICK  w{1,5} x z{6,10} x V{3,10} x entry{mkt,lim} x tp{0.3,0.6} x stop{0.5,1.0} x hold{30,240} x side{both,long} = 256
        fixed: cooldown 30 min after every exit, limit entry 25% of the move beyond the signal close, valid 15 min
  GRID  spacing{0.5%,1%,2%} x levels/side{5,10} x centre{fixed, EMA 24h, EMA 168h} x break{stop,hold} = 36
  ORB   session{asia,eu,us} x range{15,30,60} min x stop{opposite side, mid} x tp{none,1x,2x range} x filter{none,narrow} = 108
Selection only on IS = 2022-01-01..2024-12-31; OOS = 2025-01-01..2026-08-31, each window restarts flat at equity 1.
Usage: python run.py [main|zerocost]
"""
import itertools, json, os, sys, time
import numpy as np, pandas as pd
from multiprocessing import Pool
from common import *
from signals import wick_events, sessions, narrow_flag
from kernels import wick_sim, grid_sim, orb_sim

MODE = sys.argv[1] if len(sys.argv) > 1 else 'main'
ZERO = MODE == 'zerocost'          # no fees, no slippage (gross edge diagnostic), L=1 only
NORANGE = MODE == 'norange'        # fees + base slippage, without the fast-market range add-on (sensitivity)
FT, FM, RS = (0.0, 0.0, 0.0) if ZERO else (FEE_T, FEE_M, 0.0 if NORANGE else RANGE_SLIP)
LEVS_RUN = [1] if ZERO else LEVS
OUT = os.path.join(HERE, 'out'); os.makedirs(OUT, exist_ok=True)

WICK_GRID = [dict(w=w, z=z, V=V, entry=e, tp=f, stop=s, hold=H, side=sd)
             for w, z, V, e, f, s, H, sd in itertools.product((1, 5), (6, 10), (3, 10), ('mkt', 'lim'), (0.3, 0.6), (0.5, 1.0), (30, 240), ('both', 'long'))]
GRID_GRID = [dict(g=g, N=N, centre=cn, brk=b) for g, N, cn, b in itertools.product((0.005, 0.01, 0.02), (5, 10), ('fixed', 'ema24', 'ema168'), ('stop', 'hold'))]
ORB_GRID = [dict(session=s, M=M, stop=st, tp=tp, filt=f) for s, M, st, tp, f in itertools.product(('asia', 'eu', 'us'), (15, 30, 60), ('opp', 'mid'), (0, 1, 2), ('none', 'narrow'))]
FIXED = dict(cooldown=30, lim_k=0.25, lim_valid=15)

if MODE == 'main':
    json.dump(dict(written=pd.Timestamp.now('UTC').isoformat(), wick=WICK_GRID, grid=GRID_GRID, orb=ORB_GRID, fixed=FIXED, levs=LEVS,
                   is_window=[IS0, IS1], oos_window=[OOS0, OOS1], fee_t=FEE_T, fee_m=FEE_M, range_slip=RANGE_SLIP, base_slip=BASE_SLIP,
                   tick=TICK, mmr=MMR, coins=COINS,
                   selection=['S1: per family and per L, the config with the highest IS portfolio CAGR',
                              'S2: per family, the config with the highest IS Sharpe at L=1; its leverage = argmax IS CAGR over L']),
              open(os.path.join(HERE, 'prereg.json'), 'w'), indent=1) if not os.path.exists(os.path.join(HERE, 'prereg.json')) else None

t0 = time.time()
D = {}
for coin in COINS:
    d = load(coin)
    D[coin] = d
WIN = {p: window(D['BTC'], a, b) for p, (a, b) in dict(IS=(IS0, IS1), OOS=(OOS0, OOS1)).items()}
for coin in COINS:
    assert window(D[coin], IS0, IS1) == WIN['IS'] and window(D[coin], OOS0, OOS1) == WIN['OOS']
DAYS = {p: pd.to_datetime(np.unique(D['BTC']['day'][a:b]) * 86_400_000, unit='ms') for p, (a, b) in WIN.items()}
MONTHS = {p: (x.year * 12 + x.month).values for p, x in DAYS.items()}
EV = {}
for coin in COINS:
    for w, z, V in itertools.product((1, 5), (6, 10), (3, 10)):
        EV[(coin, w, z, V)] = wick_events(D[coin], w, z, V)
SES = {}
for coin in COINS:
    for s in ('asia', 'eu', 'us'):
        for M in (15, 30, 60):
            arm, until, ex, orh, orl, width, clean = sessions(D[coin], s, M)
            nar = narrow_flag(width)
            SES[(coin, s, M)] = (arm, until, ex, orh, orl, clean.astype(np.int64), (clean & (nar == 1)).astype(np.int64))
print(f'prep {time.time() - t0:.0f}s', flush=True)


def run_sleeve(fam, cfg, coin, L, per):
    d = D[coin]; i0, i1 = WIN[per]
    nd = len(DAYS[per])
    r = np.zeros(nd); tr = np.ones(nd); st = np.zeros(8, np.int64)
    trades = np.zeros(20000)
    bs = 0.0 if ZERO else BASE_SLIP[coin]
    args = (d['o'], d['h'], d['l'], d['c'], d['mh'], d['ml'], d['mc'], d['fund'], d['fflag'], d['day'], d['month'], i0, i1)
    ntr = 0
    if fam == 'wick':
        idx, dr, mf = EV[(coin, cfg['w'], cfg['z'], cfg['V'])]
        ntr = wick_sim(*args, idx, dr, mf, TICK[coin], MMR[coin], bs, float(L), cfg['entry'] == 'lim', cfg['tp'], cfg['stop'],
                       cfg['hold'], cfg['side'] == 'long', FIXED['cooldown'], FIXED['lim_k'], FIXED['lim_valid'],
                       FT, FM, RS, CAP_FRAC, r, tr, st, trades)
    elif fam == 'grid':
        W = {'fixed': 0.0, 'ema24': 24.0, 'ema168': 168.0}[cfg['centre']]
        grid_sim(*args, TICK[coin], MMR[coin], bs, float(L), cfg['g'], cfg['N'], W, cfg['brk'] == 'stop', FT, FM, RS, r, tr, st)
    else:
        arm, until, ex, orh, orl, ok_all, ok_nar = SES[(coin, cfg['session'], cfg['M'])]
        ok = ok_all if cfg['filt'] == 'none' else ok_nar
        ntr = orb_sim(*args, arm, until, ex, orh, orl, ok, TICK[coin], MMR[coin], bs, float(L), cfg['stop'] == 'mid', float(cfg['tp']),
                      FT, FM, RS, CAP_FRAC, r, tr, st, trades)
    return r, tr, st, trades[:min(ntr, len(trades))]


def job(args):
    fam, ci = args[0], args[1]
    cfg = args[2] if len(args) > 2 else {'wick': WICK_GRID, 'grid': GRID_GRID, 'orb': ORB_GRID}[fam][ci]
    rows = []
    for L in LEVS_RUN:
        for per in ('IS', 'OOS'):
            sl = []; stats = np.zeros(8, np.int64); coin_cagr = {}; tr_all = []
            for coin in COINS:
                r, tr, st, trades = run_sleeve(fam, cfg, coin, L, per)
                sl.append((r, tr)); stats += st; tr_all.append(trades)
                v = np.cumprod(1 + r)
                coin_cagr[coin] = round(float(v[-1] ** (365.25 / len(r)) - 1) if v[-1] > 0 else -1.0, 4)
            V, TR = combine(sl, None, MONTHS[per])
            m = metrics(V, TR, DAYS[per])
            ta = np.concatenate(tr_all) if tr_all else np.zeros(0)
            row = dict(family=fam, cfg_id=ci, cfg=json.dumps(cfg), L=L, period=per, **{k: v for k, v in m.items() if k != 'per_year'},
                       per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()}),
                       coin_cagr=json.dumps(coin_cagr))
            if fam == 'grid':
                row.update(fills=int(stats[0]), liq=int(stats[1]), stops=int(stats[2]), rebal=int(stats[3]), trades=int(stats[0] + stats[2] + stats[3]))
            else:
                row.update(trades=int(stats[0]), liq=int(stats[1]), stops=int(stats[2]), tps=int(stats[3]), time_exits=int(stats[4]),
                           lim_placed=int(stats[5]), lim_filled=int(stats[6]),
                           mean_trade=float(ta.mean()) if len(ta) else 0.0,
                           t_trade=float(ta.mean() / ta.std() * np.sqrt(len(ta))) if len(ta) > 2 and ta.std() > 0 else 0.0,
                           win_rate=float((ta > 0).mean()) if len(ta) else 0.0)
            rows.append(row)
    return rows


if __name__ == '__main__':
    fams = sys.argv[2].split(',') if len(sys.argv) > 2 else ['wick', 'grid', 'orb']
    jobs = [(f, i) for f in fams for i in range(len({'wick': WICK_GRID, 'grid': GRID_GRID, 'orb': ORB_GRID}[f]))]
    if os.environ.get('SMOKE'):
        jobs = jobs[:int(os.environ['SMOKE'])]
    t1 = time.time()
    rows = []
    with Pool(int(os.environ.get('NPROC', 4))) as pool:
        for k, rr in enumerate(pool.imap_unordered(job, jobs, chunksize=1)):
            rows.extend(rr)
            if k % 20 == 0:
                print(f'{k + 1}/{len(jobs)} {time.time() - t1:.0f}s', flush=True)
    df = pd.DataFrame(rows)
    tag = ('' if MODE == 'main' else MODE + '_') + '_'.join(fams)
    if os.environ.get('SMOKE'):
        tag = 'smoke_' + tag
    df.to_csv(os.path.join(OUT, f'grid_{tag}.csv'), index=False)
    print('done', len(df), f'{time.time() - t1:.0f}s')
