"""Independent pure-Python re-implementations (written from the rule text, explicit order lists, no shared code with
kernels.py except the data and the event list) compared with the numba kernels on single sleeves."""
import sys, math, json
sys.argv = ['run.py', 'main']
import numpy as np, pandas as pd
import run
from common import TICK, MMR, BASE_SLIP, FEE_T, FEE_M, RANGE_SLIP, CAP_FRAC

def liqp(side, Q, P0, Ea, mmr):
    # equity at mark m: Ea + side*Q*(m-P0) = (mmr+fee_t)*Q*m
    return (Q * P0 - Ea) / (Q * (1 - mmr - FEE_T)) if side > 0 else (Ea + Q * P0) / (Q * (1 + mmr + FEE_T))

def py_wick(coin, cfg, L, per):
    d = run.D[coin]; a, b = run.WIN[per]; tick, mmr, bs = TICK[coin], MMR[coin], BASE_SLIP[coin]
    idx, dr, mf = run.EV[(coin, cfg['w'], cfg['z'], cfg['V'])]
    ev = {int(i): (int(s), float(m)) for i, s, m in zip(idx, dr, mf)}
    E = 1.0; pos = None; pend = None; cool = 0; dead_m = None; ntr = 0; nliq = 0
    o, h, l, c, mh, ml, mc, fu, ff, mo = d['o'], d['h'], d['l'], d['c'], d['mh'], d['ml'], d['mc'], d['fund'], d['fflag'], d['month']
    for i in range(a, b):
        if dead_m is not None:
            if mo[i] != dead_m and d['day'][i] != d['day'][i - 1]:
                dead_m = None; E = 1.0; pos = None; pend = None; cool = 0
            else:
                continue
        sl = bs + RANGE_SLIP * (h[i] - l[i]) / o[i]
        if pos and ff[i]:
            pos['Ea'] -= pos['s'] * fu[i] * pos['Q'] * mc[i - 1]; pos['liq'] = liqp(pos['s'], pos['Q'], pos['P0'], pos['Ea'], mmr)
        closed = False
        if pos and i - pos['t'] >= cfg['hold']:
            px = o[i] * (1 - pos['s'] * sl); E = pos['Ea'] + pos['s'] * pos['Q'] * (px - pos['P0']) - FEE_T * pos['Q'] * px; pos = None; closed = True
        def open_pos(s, P0, fee, m):
            Q = L * E / P0; Ea = E - fee * L * E; lq = liqp(s, Q, P0, Ea, mmr)
            ld = (P0 - lq) / P0 if s > 0 else (lq - P0) / P0
            if ld <= 0 or lq <= 0: ld = 1.0
            sd = min(cfg['stop'] * m, CAP_FRAC * ld)
            return dict(s=s, P0=P0, Q=Q, Ea=Ea, liq=lq, stop=P0 * (1 - s * sd), tp=P0 * (1 + s * cfg['tp'] * m), t=i)
        if pos is None and not closed and pend is None and i >= cool and (i - 1) in ev:
            s, m = ev[i - 1]
            if not (cfg['side'] == 'long' and s < 0):
                if cfg['entry'] == 'lim':
                    pend = dict(s=s, px=c[i - 1] * (1 - s * 0.25 * m), until=i + 15, m=m)
                else:
                    pos = open_pos(s, o[i] * (1 + s * sl), FEE_T, m)
        fillbar = False
        if pos is None and pend is not None and not closed:
            if i >= pend['until']:
                pend = None
            elif (pend['s'] > 0 and l[i] <= pend['px'] - tick) or (pend['s'] < 0 and h[i] >= pend['px'] + tick):
                pos = open_pos(pend['s'], pend['px'], FEE_M, pend['m']); pend = None; fillbar = True
        if pos and not closed:
            s = pos['s']
            lh = ml[i] <= pos['liq'] if s > 0 else mh[i] >= pos['liq']
            sh = l[i] <= pos['stop'] if s > 0 else h[i] >= pos['stop']
            th = (not fillbar) and (h[i] >= pos['tp'] + tick if s > 0 else l[i] <= pos['tp'] - tick)
            if lh:
                nliq += 1; ntr += 1; E = 0.0; pos = None; dead_m = mo[i]; continue
            if sh:
                sp = pos['stop'] if fillbar else (min(o[i], pos['stop']) if s > 0 else max(o[i], pos['stop']))
                px = sp * (1 - s * sl); E = pos['Ea'] + s * pos['Q'] * (px - pos['P0']) - FEE_T * pos['Q'] * px; pos = None; closed = True
            elif th:
                px = pos['tp']; E = pos['Ea'] + s * pos['Q'] * (px - pos['P0']) - FEE_M * pos['Q'] * px; pos = None; closed = True
        if closed:
            ntr += 1; cool = i + 30; E = max(E, 0.0)
            if E <= 1e-12:
                dead_m = mo[i]
    if pos:
        i = b - 1; sl = bs + RANGE_SLIP * (h[i] - l[i]) / o[i]; px = c[i] * (1 - pos['s'] * sl)
        E = pos['Ea'] + pos['s'] * pos['Q'] * (px - pos['P0']) - FEE_T * pos['Q'] * px
    return E, ntr, nliq

def py_grid(coin, cfg, L, per):
    """explicit order lists; anchor/EMA hourly; conservative liquidation/stop."""
    d = run.D[coin]; a, b = run.WIN[per]; tick, mmr, bs = TICK[coin], MMR[coin], BASE_SLIP[coin]
    o, h, l, c, mh, ml, mc, fu, ff, mo = d['o'], d['h'], d['l'], d['c'], d['mh'], d['ml'], d['mc'], d['fund'], d['fflag'], d['month']
    g, N = cfg['g'], cfg['N']; W = {'fixed': 0, 'ema24': 24, 'ema168': 168}[cfg['centre']]; stopmode = cfg['brk'] == 'stop'
    C = 1.0; inv = 0; u = 0.0; A = None; ema = None; restart = True; dead_m = None; nfill = 0; nliq = 0; nstop = 0
    daily = {}
    for i in range(a, b):
        if dead_m is not None:
            if mo[i] != dead_m and d['day'][i] != d['day'][i - 1]:
                dead_m = None; C = 1.0; inv = 0; restart = True
            else:
                continue
        sl = bs + RANGE_SLIP * (h[i] - l[i]) / o[i]
        if restart:
            A = ema = o[i]; inv = 0; u = L * C / (N * A); restart = False
        if inv and ff[i]:
            C -= inv * u * mc[i - 1] * fu[i]
        lvl = lambda j: A * (1 - j * g)
        if W and i % 60 == 0 and i > a:
            ema = ema + 2 / (W + 1) * (c[i - 1] - ema); A = ema
            p = o[i]
            if stopmode and (p <= lvl(N + 1) or p >= lvl(-N - 1)):
                if inv:
                    px = p * (1 - sl) if inv > 0 else p * (1 + sl)
                    C += inv * u * px - FEE_T * abs(inv) * u * px; nstop += 1
                inv = 0; restart = True; continue
            if inv < N and p <= lvl(inv + 1):
                target = min(N, math.floor((A - p) / (g * A))); n = target - inv; px = p * (1 + sl)
                C -= n * u * px * (1 + FEE_T); inv = target
            elif inv > -N and p >= lvl(inv - 1):
                target = max(-N, math.ceil((A - p) / (g * A))); n = inv - target; px = p * (1 - sl)
                C += n * u * px * (1 - FEE_T); inv = target
        buys = [lvl(j) for j in range(inv + 1, N + 1)]          # resting at bar start
        sells = [lvl(j) for j in range(inv - 1, -N - 1, -1)]
        fb = [x for x in buys if l[i] <= x - tick * (1 - 1e-9 * 0) - 1e-12 * x or abs(l[i] - (x - tick)) < 1e-9 * x]
        fs = [x for x in sells if h[i] >= x + tick - 1e-9 * x]
        fb = [x for x in buys if l[i] <= x - tick + 1e-9 * x]
        Bc = sum(fb) * u; Sc = sum(fs) * u
        Cl, ql = C - Bc * (1 + FEE_M), inv + len(fb)
        Ch, qh = C + Sc * (1 - FEE_M), inv - len(fs)
        liq = (ql > 0 and Cl + ql * u * ml[i] <= (mmr + FEE_T) * ql * u * ml[i]) or (qh < 0 and Ch + qh * u * mh[i] <= (mmr + FEE_T) * (-qh) * u * mh[i])
        if liq:
            nliq += 1; C = 0.0; inv = 0; dead_m = mo[i]; continue
        s_lo = stopmode and l[i] <= lvl(N + 1); s_hi = stopmode and h[i] >= lvl(-N - 1)
        if s_lo or s_hi:
            outs = []
            if s_lo:
                px = min(o[i], lvl(N + 1)) * (1 - sl); outs.append(Cl + ql * u * px - FEE_T * abs(ql) * u * px)
            if s_hi:
                px = max(o[i], lvl(-N - 1)) * (1 + sl); outs.append(Ch + qh * u * px - FEE_T * abs(qh) * u * px)
            C = min(outs); inv = 0; restart = True; nstop += 1; nfill += len(fb) + len(fs)
            if C <= 1e-12:
                C = 0.0; dead_m = mo[i]
            continue
        C = C - Bc * (1 + FEE_M) + Sc * (1 - FEE_M); inv = inv + len(fb) - len(fs); nfill += len(fb) + len(fs)
        if inv == 0:
            u = L * C / (N * A)
    i = b - 1
    if dead_m is not None:
        return 0.0, nfill, nliq, nstop
    sl = bs + RANGE_SLIP * (h[i] - l[i]) / o[i]
    px = c[i] * (1 - sl) if inv > 0 else c[i] * (1 + sl)
    return C + inv * u * px - FEE_T * abs(inv) * u * px, nfill, nliq, nstop

def kernel_final(fam, cfg, coin, L, per):
    r, tr, st, trades = run.run_sleeve(fam, cfg, coin, L, per)
    return float(np.prod(1 + r)), st

checks = [
    ('wick', dict(w=5, z=10, V=10, entry='lim', tp=0.6, stop=1.0, hold=30, side='long'), 'SOL', 10, 'OOS'),
    ('wick', dict(w=1, z=6, V=3, entry='mkt', tp=0.3, stop=0.5, hold=240, side='both'), 'BTC', 3, 'OOS'),
    ('wick', dict(w=5, z=10, V=10, entry='mkt', tp=1e6, stop=1e6, hold=60, side='long'), 'AVAX', 5, 'IS'),
    ('grid', dict(g=0.01, N=10, centre='ema24', brk='hold'), 'ETH', 3, 'OOS'),
    ('grid', dict(g=0.005, N=5, centre='fixed', brk='stop'), 'BTC', 1, 'OOS'),
    ('grid', dict(g=0.02, N=5, centre='ema168', brk='stop'), 'DOGE', 5, 'OOS'),
]
for fam, cfg, coin, L, per in checks:
    kf, st = kernel_final(fam, cfg, coin, L, per)
    if fam == 'wick':
        E, ntr, nliq = py_wick(coin, cfg, L, per)
        print(fam, coin, L, per, json.dumps(cfg), f'kernel final {kf:.6f} trades {st[0]} liq {st[1]} | python final {E:.6f} trades {ntr} liq {nliq}')
    else:
        E, nf, nliq, nstop = py_grid(coin, cfg, L, per)
        print(fam, coin, L, per, json.dumps(cfg), f'kernel final {kf:.6f} fills {st[0]} liq {st[1]} stops {st[2]} | python final {E:.6f} fills {nf} liq {nliq} stops {nstop}')
