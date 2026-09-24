# Independent pure-python re-implementation of the execution rules, compared with the numba sim.
import numpy as np, pandas as pd
from bt import *
def ref(d, i0, i1, evL, evS, exL, exS, stopf, vm, use_vol, L, tpm, mh, cons):
    o,h,l,c,mo,mh_,ml = [d[k].values for k in ['o','h','l','c','mo','mh','ml']]
    fr = d.fund.values; ff = d.fund_flag.values
    E = 1.0; pos = 0; nl = 0; nt = 0
    for i in range(i0, i1):
        if pos:
            if ff[i]:
                Ea -= pos * fr[i] * Q * mo[i]
            liqpx = (Q*P0 - Ea)/(Q*(1-MMR-FEE_T)) if pos == 1 else (Ea + Q*P0)/(Q*(1+MMR+FEE_T))
            adv_mark_open = (mo[i] <= liqpx) if pos == 1 else (mo[i] >= liqpx)
            if adv_mark_open:
                return 0.0, nt+1, nl+1
            fill = None
            if (pos == 1 and o[i] <= stop) or (pos == -1 and o[i] >= stop):
                fill, fee = o[i]*(1-STOP_SLIP*pos), FEE_T
            elif (pos == 1 and o[i] >= tp) or (pos == -1 and o[i] <= tp):
                fill, fee = tp, FEE_M
            elif (pos == 1 and (exL[i] or evS[i])) or (pos == -1 and (exS[i] or evL[i])) or (mh and i - ent >= mh):
                fill, fee = o[i]*(1-SLIP*pos), FEE_T
            if fill is not None:
                E = Ea + pos*Q*(fill-P0) - fee*Q*fill; pos = 0; nt += 1
                if E <= 1e-9: return 0.0, nt, nl
        if not pos and (evL[i] or evS[i]):
            pos = 1 if evL[i] else -1
            lev = L * (vm[i] if use_vol else 1.0)
            P0 = o[i]*(1+SLIP*pos); Q = lev*E/P0; Ea = E - FEE_T*lev*E; ent = i
            liqpx = (Q*P0 - Ea)/(Q*(1-MMR-FEE_T)) if pos == 1 else (Ea + Q*P0)/(Q*(1+MMR+FEE_T))
            liqd = pos*(P0 - liqpx)/P0
            if pos == 1 and liqpx <= 0: liqd = 1.0
            sd = min(stopf[i], CAP_FRAC*liqd)
            stop = P0*(1-pos*sd)
            tp = P0*(1+pos*tpm*sd) if tpm > 0 else (1e18 if pos == 1 else -1.0)
        if pos:
            liqpx = (Q*P0 - Ea)/(Q*(1-MMR-FEE_T)) if pos == 1 else (Ea + Q*P0)/(Q*(1+MMR+FEE_T))
            if pos == 1:
                lh, sh, th = ml[i] <= liqpx, l[i] <= stop, h[i] > tp
                pen = (stop - l[i])/stop
            else:
                lh, sh, th = mh_[i] >= liqpx, h[i] >= stop, l[i] < tp
                pen = (h[i] - stop)/stop
            if lh and (cons or not sh):
                return 0.0, nt+1, nl+1
            fill = None
            if sh: fill, fee = stop*(1-pos*(STOP_SLIP+STOP_IMPACT*pen)), FEE_T
            elif th: fill, fee = tp, FEE_M
            if fill is not None:
                E = Ea + pos*Q*(fill-P0) - fee*Q*fill; pos = 0; nt += 1
                if E <= 1e-9: return 0.0, nt, nl
    if pos:
        px = c[i1-1]*(1-SLIP*pos); E = Ea + pos*Q*(px-P0) - FEE_T*Q*px; nt += 1
    return E, nt, nl

for sym in ['BTCUSDT', 'ETHUSDT']:
    d = load(sym); t = d.t.values; n5 = len(d)
    day = ((t - t[0]) // 86400000).astype(np.int64)
    arrs = [d[k].values.astype(np.float64) for k in ['o','h','l','c','mo','mh','ml','fund']]
    ff = d.fund_flag.values.astype(np.int8)
    vm, _ = vol_mult(d, ms(IS0), ms(IS1))
    for (tf, fam, p, stopkind, tpm, uv, L, cons, a0, a1) in [
        ('15m','ema',(20,100),'fix',2.0,0,20,True, IS0, IS1),
        ('1h','mr',2.0,'atr',0.0,1,10,True, OOS0, OOS1),
        ('4h','donch',20,'atr',2.0,0,20,False, IS0, IS1),
        ('4h','donch',20,'atr',0.0,0,15,True, OOS0, OOS1),
        ('1h','ema',(50,200),'fix',0.0,1,5,True, IS0, IS1),
        ('15m','mr',3.0,'atr',2.0,0,3,True, IS0, IS1)]:
        i0 = int(np.searchsorted(t, ms(a0))); i1 = int(np.searchsorted(t, ms(a1)))
        d0 = int(day[i0]); nd = int(day[i1-1]-d0+1)
        a, b_, c_, d_, atr, mh = signals(d, tf, fam, p)
        stopf = np.full(n5, 0.015) if stopkind == 'fix' else np.nan_to_num(2*atr, nan=0.015)
        r = run_one(*arrs, ff, day, i0, i1, d0, nd, a, b_, c_, d_, stopf, vm, uv == 1, float(L), tpm, mh, cons, False)
        rr = ref(d, i0, i1, a, b_, c_, d_, stopf, vm, uv == 1, L, tpm, mh, cons)
        print(sym, tf, fam, p, stopkind, tpm, uv, L, cons, a0, '| numba final %.6g trades %d liq %d | ref final %.6g trades %d liq %d' % (r[0][-1], r[2], r[3], rr[0], rr[1], rr[2]))
