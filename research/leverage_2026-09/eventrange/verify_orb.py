"""Independent pure-Python ORB re-implementation vs orb_sim (single sleeves)."""
import sys, json
sys.argv = ['run.py', 'main']
import numpy as np, run
from common import TICK, MMR, BASE_SLIP, FEE_T, FEE_M, RANGE_SLIP, CAP_FRAC
def py_orb(coin, cfg, L, per):
    d = run.D[coin]; a, b = run.WIN[per]; tick, mmr, bs = TICK[coin], MMR[coin], BASE_SLIP[coin]
    arm, until, ex, orh, orl, ok_all, ok_nar = run.SES[(coin, cfg['session'], cfg['M'])]
    ok = ok_all if cfg['filt'] == 'none' else ok_nar
    sess = {int(x): (int(u), int(e), float(hh), float(ll), int(k)) for x, u, e, hh, ll, k in zip(arm, until, ex, orh, orl, ok)}
    o, h, l, c, mh, ml, mc, fu, ff, mo = d['o'], d['h'], d['l'], d['c'], d['mh'], d['ml'], d['mc'], d['fund'], d['fflag'], d['month']
    E = 1.0; pos = None; armed = None; dead_m = None; n = 0; nl = 0
    for i in range(a, b):
        if dead_m is not None:
            if mo[i] != dead_m and d['day'][i] != d['day'][i - 1]:
                dead_m = None; E = 1.0; pos = None; armed = None
            else:
                continue
        sl = bs + RANGE_SLIP * (h[i] - l[i]) / o[i]
        if i in sess:
            u_, e_, hh, ll, k = sess[i]
            armed = dict(until=u_, ex=e_, hi=hh, lo=ll) if (pos is None and k == 1) else None
        if pos and ff[i]:
            pos['Ea'] -= pos['s'] * fu[i] * pos['Q'] * mc[i - 1]
            s = pos['s']; pos['liq'] = (pos['Q'] * pos['P0'] - pos['Ea']) / (pos['Q'] * (1 - mmr - FEE_T)) if s > 0 else (pos['Ea'] + pos['Q'] * pos['P0']) / (pos['Q'] * (1 + mmr + FEE_T))
        closed = False; entry = False
        if pos and i >= pos['ex']:
            px = o[i] * (1 - pos['s'] * sl); E = pos['Ea'] + pos['s'] * pos['Q'] * (px - pos['P0']) - FEE_T * pos['Q'] * px; pos = None; closed = True
        if armed and pos is None and not closed:
            if i >= armed['until']:
                armed = None
            else:
                up = h[i] >= armed['hi'] + tick; dn = l[i] <= armed['lo'] - tick
                s = 0
                if up and dn: s = 1 if armed['hi'] - o[i] <= o[i] - armed['lo'] else -1
                elif up: s = 1
                elif dn: s = -1
                if s:
                    P0 = max(o[i], armed['hi'] + tick) * (1 + sl) if s > 0 else min(o[i], armed['lo'] - tick) * (1 - sl)
                    Q = L * E / P0; Ea = E - FEE_T * L * E
                    lq = (Q * P0 - Ea) / (Q * (1 - mmr - FEE_T)) if s > 0 else (Ea + Q * P0) / (Q * (1 + mmr + FEE_T))
                    ld = (P0 - lq) / P0 if s > 0 else (lq - P0) / P0
                    if ld <= 0 or lq <= 0: ld = 1.0
                    ref = (armed['hi'] + armed['lo']) / 2 if cfg['stop'] == 'mid' else (armed['lo'] if s > 0 else armed['hi'])
                    sd = min(abs(P0 - ref) / P0, CAP_FRAC * ld); sd = max(sd, 2 * tick / P0)
                    W = armed['hi'] - armed['lo']
                    pos = dict(s=s, P0=P0, Q=Q, Ea=Ea, liq=lq, stop=P0 * (1 - s * sd), tp=(P0 + s * cfg['tp'] * W) if cfg['tp'] else None, ex=armed['ex'])
                    armed = None; entry = True
        if pos and not closed:
            s = pos['s']
            if (ml[i] <= pos['liq']) if s > 0 else (mh[i] >= pos['liq']):
                n += 1; nl += 1; E = 0.0; pos = None; dead_m = mo[i]; armed = None; continue
            if (l[i] <= pos['stop']) if s > 0 else (h[i] >= pos['stop']):
                sp = pos['stop'] if entry else (min(o[i], pos['stop']) if s > 0 else max(o[i], pos['stop']))
                px = sp * (1 - s * sl); E = pos['Ea'] + s * pos['Q'] * (px - pos['P0']) - FEE_T * pos['Q'] * px; pos = None; closed = True
            elif pos['tp'] is not None and not entry and ((h[i] >= pos['tp'] + tick) if s > 0 else (l[i] <= pos['tp'] - tick)):
                px = pos['tp']; E = pos['Ea'] + s * pos['Q'] * (px - pos['P0']) - FEE_M * pos['Q'] * px; pos = None; closed = True
        if closed:
            n += 1; E = max(E, 0.0)
            if E <= 1e-12: dead_m = mo[i]; armed = None
    if pos:
        i = b - 1; sl = bs + RANGE_SLIP * (h[i] - l[i]) / o[i]; px = c[i] * (1 - pos['s'] * sl)
        E = pos['Ea'] + pos['s'] * pos['Q'] * (px - pos['P0']) - FEE_T * pos['Q'] * px
    return E, n, nl
for cfg, coin, L, per in [(dict(session='us', M=60, stop='opp', tp=1, filt='none'), 'ETH', 5, 'IS'),
                          (dict(session='asia', M=15, stop='mid', tp=0, filt='narrow'), 'DOGE', 10, 'OOS'),
                          (dict(session='eu', M=30, stop='opp', tp=2, filt='none'), 'SOL', 20, 'OOS')]:
    r, tr, st, _ = run.run_sleeve('orb', cfg, coin, L, per)
    k = np.nonzero(r <= -0.999)[0]
    kf = np.prod(1 + r[(k[-1] + 1 if len(k) else 0):])
    E, n, nl = py_orb(coin, cfg, L, per)
    print(json.dumps(cfg), coin, L, per, f'kernel (post-last-liq) {kf:.6f} trades {st[0]} liq {st[1]} | python {E:.6f} trades {n} liq {nl}')
