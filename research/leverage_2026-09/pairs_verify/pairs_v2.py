"""Pairs stat-arb, version 2 simulator: adds (i) maker execution and (ii) a 1-minute liquidation bound.

Everything else (data, walk-forward universe/pair selection, signals, costs, protocol) is in pairs_bt.py.

EXECUTION
  taker : entries and all exits at the next bar open, taker fee 0.05% per leg + slippage (pairs_bt.py).
  maker : entries and take-profit exits are post-only limit orders on both legs at the signal bar's close price,
          live for one bar.  A leg fills only if the next bar trades THROUGH the limit by >= 1 bp (buy: low <=
          limit*(1-1bp); sell: high >= limit*(1+1bp)); maker fee 0.02%, no slippage.  If only one leg fills, the other
          leg is completed at that bar's close with a taker order (fee + slippage) -> legging cost is paid.  If
          neither fills the order is cancelled (opportunity cost) and the signal is re-evaluated at the close.
          Stops, time stops, forced exits: taker at the next open.
LIQUIDATION / INTRABAR DRAWDOWN
  h1 : hourly bound: long legs at the hour's low, short legs at the hour's high (mark or last, the wider).
  m1 : 1-minute bound (minute_bound.py): within each minute the long leg at its 1m MARK low and the short leg at its
       1m MARK high; per slot the worst minute of the hour; slots summed (conservative).  Falls back to h1 for a
       slot-bar when a minute is missing or the live notional ratio is outside [0.8, 1.25]*beta.
       Maintenance margin evaluated at the hour's high for every leg (conservative).
"""
import os, sys, json, time
import numpy as np, pandas as pd
from numba import njit
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_bt import *            # noqa
import minute_bound

TICK = 0.0001
FEE_M = 0.0002


@njit(cache=True)
def _close_leg_px(q, px, sl):
    # closing a long sells (receive less), closing a short buys (pay more)
    return px * (1.0 - sl) if q > 0 else px * (1.0 + sl)


@njit(cache=True)
def sim2(O, H, L, C, MH, ML, FR, DAY, i0, i1, nd, PA, PB, BE, Z, SA, SB, NEWM, WL, WS, mmr, imr,
         zin, zout, zstop, maxhold, lev, sleeve, fee, feem, rslip, maker, use_w, rec):
    K = PA.shape[0]
    nsym = O.shape[0]
    lastc = np.full(nsym, np.nan)
    pos = np.zeros(K, np.int64); qA = np.zeros(K); qB = np.zeros(K); eA = np.zeros(K); eB = np.zeros(K)
    ia = np.full(K, -1, np.int64); ib = np.full(K, -1, np.int64); tent = np.zeros(K, np.int64)
    pend = np.zeros(K, np.int64); blocked = np.zeros(K, np.int64)
    lpa = np.zeros(K); lpb = np.zeros(K)
    cash = np.zeros(K)
    if sleeve:
        for k in range(K):
            cash[k] = 1.0 / K
    else:
        cash[0] = 1.0
    daily = np.full(nd, np.nan)
    ntr = 0; nliq = 0; nstop = 0; ntime = 0; nforce = 0; nleg = 0; nmiss = 0; nfb = 0; nwb = 0
    fees = 0.0; fund = 0.0; bars_in = 0
    peak = 1.0; maxdd = 0.0; dead = False
    TR = np.zeros((rec, 9))
    nrec = 0
    s_eq0 = np.zeros(K); s_cost = np.zeros(K); s_why = np.zeros(K, np.int64)
    grosssum = 0.0
    for i in range(i0, i1):
        for s in range(nsym):
            if not np.isnan(C[s, i]):
                lastc[s] = C[s, i]
        if dead:
            daily[DAY[i]] = 0.0
            continue
        # ---- 1. exits
        for k in range(K):
            if pos[k] == 0:
                continue
            kk = k if sleeve else 0
            force = (PA[k, i] != ia[k]) or (PB[k, i] != ib[k])
            a = ia[k]; b = ib[k]
            done = False
            pnl = 0.0; fe = 0.0
            if force or pend[k] == 2:
                pa = O[a, i]; pb = O[b, i]
                xa = 0.0; xb = 0.0
                if np.isnan(pa):
                    pa = lastc[a]; xa = 0.01
                if np.isnan(pb):
                    pb = lastc[b]; xb = 0.01
                ra = H[a, i] - L[a, i]; rb = H[b, i] - L[b, i]
                sla = SA[k, i - 1] + xa + (rslip * ra / pa if not np.isnan(ra) else 0.0)
                slb = SB[k, i - 1] + xb + (rslip * rb / pb if not np.isnan(rb) else 0.0)
                fa = _close_leg_px(qA[k], pa, sla); fb = _close_leg_px(qB[k], pb, slb)
                pnl = qA[k] * (fa - eA[k]) + qB[k] * (fb - eB[k])
                fe = fee * (abs(qA[k]) * fa + abs(qB[k]) * fb)
                done = True
            elif pend[k] == 3:
                # maker take-profit on both legs at the signal-bar close
                pend[k] = 0
                if qA[k] > 0:
                    fillA = H[a, i] >= lpa[k] * (1.0 + TICK)
                else:
                    fillA = L[a, i] <= lpa[k] * (1.0 - TICK)
                if qB[k] > 0:
                    fillB = H[b, i] >= lpb[k] * (1.0 + TICK)
                else:
                    fillB = L[b, i] <= lpb[k] * (1.0 - TICK)
                if fillA or fillB:
                    if fillA:
                        fa = lpa[k]; fea = feem * abs(qA[k]) * fa
                    else:
                        ca = C[a, i]; ra = H[a, i] - L[a, i]; xa = 0.0
                        if np.isnan(ca):
                            ca = lastc[a]; ra = 0.0; xa = 0.01      # missing bar: last known price + 1% slippage
                        fa = _close_leg_px(qA[k], ca, SA[k, i] + xa + rslip * ra / ca); fea = fee * abs(qA[k]) * fa
                    if fillB:
                        fb = lpb[k]; feb = feem * abs(qB[k]) * fb
                    else:
                        cb = C[b, i]; rb = H[b, i] - L[b, i]; xb = 0.0
                        if np.isnan(cb):
                            cb = lastc[b]; rb = 0.0; xb = 0.01
                        fb = _close_leg_px(qB[k], cb, SB[k, i] + xb + rslip * rb / cb); feb = fee * abs(qB[k]) * fb
                    if not (fillA and fillB):
                        nleg += 1
                    pnl = qA[k] * (fa - eA[k]) + qB[k] * (fb - eB[k])
                    fe = fea + feb
                    done = True
                else:
                    nmiss += 1
            if done:
                cash[kk] += pnl - fe
                fees += fe
                if nrec < rec:
                    TR[nrec, 0] = k; TR[nrec, 1] = a; TR[nrec, 2] = b; TR[nrec, 3] = tent[k]
                    TR[nrec, 4] = i; TR[nrec, 5] = (pnl - fe - s_cost[k]) / s_eq0[k]
                    TR[nrec, 6] = 6 if force else s_why[k]; TR[nrec, 7] = pos[k]; TR[nrec, 8] = s_eq0[k]
                    nrec += 1
                if force:
                    nforce += 1
                pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1
        if NEWM[i] == 1:
            for k in range(K):
                blocked[k] = 0
            if sleeve:
                tot = 0.0
                eqk = np.zeros(K)
                for k in range(K):
                    e = cash[k]
                    if pos[k] != 0:
                        pa = O[ia[k], i]; pb = O[ib[k], i]
                        if np.isnan(pa):
                            pa = lastc[ia[k]]
                        if np.isnan(pb):
                            pb = lastc[ib[k]]
                        e += qA[k] * (pa - eA[k]) + qB[k] * (pb - eB[k])
                    eqk[k] = e; tot += e
                for k in range(K):
                    cash[k] += tot / K - eqk[k]
        # ---- 2. entries
        for k in range(K):
            if pend[k] != 1 and pend[k] != -1:
                continue
            d = pend[k]; pend[k] = 0
            a = PA[k, i]; b = PB[k, i]
            if a < 0 or blocked[k] == 1:
                continue
            pa = O[a, i]; pb = O[b, i]
            if np.isnan(pa) or np.isnan(pb) or np.isnan(C[a, i]) or np.isnan(C[b, i]):
                continue
            eq = 0.0; im_used = 0.0
            if sleeve:
                eq = cash[k]
            else:
                eq = cash[0]
                for j in range(K):
                    if pos[j] != 0:
                        xa_ = O[ia[j], i]; xb_ = O[ib[j], i]
                        if np.isnan(xa_):
                            xa_ = lastc[ia[j]]
                        if np.isnan(xb_):
                            xb_ = lastc[ib[j]]
                        eq += qA[j] * (xa_ - eA[j]) + qB[j] * (xb_ - eB[j])
                        im_used += abs(qA[j]) * xa_ * imr[ia[j]] + abs(qB[j]) * xb_ * imr[ib[j]]
            if eq <= 0:
                continue
            be = BE[k, i]
            per_leg = lev * eq if sleeve else lev * eq / K
            nA = 2.0 * per_leg / (1.0 + be); nB = be * nA
            need = nA * imr[a] + nB * imr[b]
            room = eq - im_used
            if room <= 0:
                continue
            if need > room:
                sc = room / need
                nA *= sc; nB *= sc
            ra = H[a, i] - L[a, i]; rb = H[b, i] - L[b, i]
            if np.isnan(ra):
                ra = 0.0
            if np.isnan(rb):
                rb = 0.0
            fea = 0.0; feb = 0.0
            if maker:
                if d == 1:
                    fillA = L[a, i] <= lpa[k] * (1.0 - TICK); fillB = H[b, i] >= lpb[k] * (1.0 + TICK)
                else:
                    fillA = H[a, i] >= lpa[k] * (1.0 + TICK); fillB = L[b, i] <= lpb[k] * (1.0 - TICK)
                if not (fillA or fillB):
                    nmiss += 1
                    continue
                if not (fillA and fillB):
                    nleg += 1
                if fillA:
                    fa = lpa[k]; fea = feem * nA
                else:
                    sla = SA[k, i] + rslip * ra / C[a, i]
                    fa = C[a, i] * (1.0 + sla) if d == 1 else C[a, i] * (1.0 - sla); fea = fee * nA
                if fillB:
                    fb = lpb[k]; feb = feem * nB
                else:
                    slb = SB[k, i] + rslip * rb / C[b, i]
                    fb = C[b, i] * (1.0 - slb) if d == 1 else C[b, i] * (1.0 + slb); feb = fee * nB
                qa_ = nA / (lpa[k] if fillA else C[a, i]); qb_ = nB / (lpb[k] if fillB else C[b, i])
            else:
                sla = SA[k, i] + rslip * ra / pa; slb = SB[k, i] + rslip * rb / pb
                if d == 1:
                    fa = pa * (1.0 + sla); fb = pb * (1.0 - slb)
                else:
                    fa = pa * (1.0 - sla); fb = pb * (1.0 + slb)
                fea = fee * nA; feb = fee * nB
                qa_ = nA / pa; qb_ = nB / pb
            if d == 1:
                qA[k] = qa_; qB[k] = -qb_
            else:
                qA[k] = -qa_; qB[k] = qb_
            eA[k] = fa; eB[k] = fb
            fe = fea + feb
            s_eq0[k] = eq; s_cost[k] = fe
            kk = k if sleeve else 0
            cash[kk] -= fe
            fees += fe
            pos[k] = d; ia[k] = a; ib[k] = b; tent[k] = i
            ntr += 1
        # ---- 3. liquidation check (conservative intrabar bound)
        anypos = False
        for k in range(K):
            if pos[k] != 0:
                anypos = True
        if anypos:
            bars_in += 1
        worst_tot = 0.0
        ew_c = cash[0]; mm_c = 0.0
        for k in range(K):
            if pos[k] == 0:
                if sleeve:
                    worst_tot += cash[k]
                continue
            a = ia[k]; b = ib[k]
            # hourly bound
            wa = ML[a, i] if qA[k] > 0 else MH[a, i]
            wb = ML[b, i] if qB[k] > 0 else MH[b, i]
            if np.isnan(wa):
                wa = lastc[a]
            if np.isnan(wb):
                wb = lastc[b]
            upl_w = qA[k] * (wa - eA[k]) + qB[k] * (wb - eB[k])
            if use_w:
                oa = O[a, i]; ob = O[b, i]
                ok = (not np.isnan(oa)) and (not np.isnan(ob)) and PA[k, i] == a and PB[k, i] == b
                if ok:
                    na = abs(qA[k]) * oa; nb = abs(qB[k]) * ob
                    r = nb / na
                    be = BE[k, i]
                    w = WL[k, i] if qA[k] > 0 else WS[k, i]
                    if (not np.isnan(w)) and r >= 0.8 * be and r <= 1.25 * be:
                        upl_w = qA[k] * (oa - eA[k]) + qB[k] * (ob - eB[k]) + na * w
                        nwb += 1
                    else:
                        nfb += 1
                else:
                    nfb += 1
            ha = MH[a, i] if not np.isnan(MH[a, i]) else lastc[a]
            hb = MH[b, i] if not np.isnan(MH[b, i]) else lastc[b]
            mm = abs(qA[k]) * ha * (mmr[a] + fee) + abs(qB[k]) * hb * (mmr[b] + fee)
            if sleeve:
                ew = cash[k] + upl_w
                if ew <= mm:
                    nliq += 1
                    if nrec < rec:
                        TR[nrec, 0] = k; TR[nrec, 1] = a; TR[nrec, 2] = b; TR[nrec, 3] = tent[k]; TR[nrec, 4] = i
                        TR[nrec, 5] = -1.0; TR[nrec, 6] = 5; TR[nrec, 7] = pos[k]; TR[nrec, 8] = s_eq0[k]
                        nrec += 1
                    cash[k] = 0.0
                    pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1
                    blocked[k] = 1
                    ew = 0.0
                worst_tot += ew
            else:
                ew_c += upl_w; mm_c += mm
        if not sleeve:
            if anypos and ew_c <= mm_c:
                nliq += 1
                if nrec < rec:
                    TR[nrec, 0] = -1; TR[nrec, 4] = i; TR[nrec, 5] = -1.0; TR[nrec, 6] = 5
                    nrec += 1
                dead = True
                cash[0] = 0.0
                for k in range(K):
                    pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1
                ew_c = 0.0
            worst_tot = ew_c
        if peak > 0:
            dd = 1.0 - worst_tot / peak
            if dd > maxdd:
                maxdd = dd
        # ---- 4. funding at the bar close
        for k in range(K):
            if pos[k] != 0:
                kk = k if sleeve else 0
                fa = FR[ia[k], i]; fb = FR[ib[k], i]
                if fa != 0.0 and not np.isnan(C[ia[k], i]):
                    x = qA[k] * C[ia[k], i] * fa
                    cash[kk] -= x; fund += x; s_cost[k] += x
                if fb != 0.0 and not np.isnan(C[ib[k], i]):
                    x = qB[k] * C[ib[k], i] * fb
                    cash[kk] -= x; fund += x; s_cost[k] += x
        # ---- 5. close equity
        eq = 0.0; gross = 0.0
        for k in range(K):
            if sleeve or k == 0:
                eq += cash[k]
            if pos[k] != 0:
                eq += qA[k] * (lastc[ia[k]] - eA[k]) + qB[k] * (lastc[ib[k]] - eB[k])
                gross += abs(qA[k]) * lastc[ia[k]] + abs(qB[k]) * lastc[ib[k]]
        if eq > 0:
            grosssum += gross / eq
        if eq > peak:
            peak = eq
        dd = 1.0 - eq / peak
        if dd > maxdd:
            maxdd = dd
        daily[DAY[i]] = eq
        if dead:
            continue
        if not sleeve and eq <= 0:
            dead = True
            continue
        # ---- 6. signals at the close
        last = (i == i1 - 1)
        for k in range(K):
            z = Z[k, i]
            if pos[k] != 0:
                ex = 0
                if last:
                    ex = 4
                elif not np.isnan(z):
                    if pos[k] == 1:
                        if z <= -zstop:
                            ex = 2
                        elif z >= -zout:
                            ex = 1
                    else:
                        if z >= zstop:
                            ex = 2
                        elif z <= zout:
                            ex = 1
                if ex == 0 and i - tent[k] >= maxhold:
                    ex = 3
                if ex != 0:
                    s_why[k] = ex
                    if ex == 1 and maker and not np.isnan(C[ia[k], i]) and not np.isnan(C[ib[k], i]):
                        pend[k] = 3; lpa[k] = C[ia[k], i]; lpb[k] = C[ib[k], i]
                    else:
                        pend[k] = 2
                    if ex == 2:
                        nstop += 1; blocked[k] = 1
                    if ex == 3:
                        ntime += 1
            elif not last and blocked[k] == 0 and PA[k, i] >= 0 and not np.isnan(z):
                if PA[k, i + 1] == PA[k, i] and PB[k, i + 1] == PB[k, i]:
                    if z <= -zin and z > -zstop:
                        pend[k] = 1
                    elif z >= zin and z < zstop:
                        pend[k] = -1
                    if pend[k] != 0:
                        lpa[k] = C[PA[k, i], i]; lpb[k] = C[PB[k, i], i]
    # settle open positions at the final close (taker)
    eq = 0.0
    for k in range(K):
        if sleeve or k == 0:
            eq += cash[k]
        if pos[k] != 0 and not dead:
            fa = _close_leg_px(qA[k], lastc[ia[k]], SA[k, i1 - 1]); fb = _close_leg_px(qB[k], lastc[ib[k]], SB[k, i1 - 1])
            fe = fee * (abs(qA[k]) * fa + abs(qB[k]) * fb)
            eq += qA[k] * (fa - eA[k]) + qB[k] * (fb - eB[k]) - fe
            fees += fe
    if dead:
        eq = 0.0
    daily[DAY[i1 - 1]] = eq
    stats = np.array([eq, maxdd, ntr, nliq, nstop, ntime, nforce, fees, fund, bars_in / (i1 - i0),
                      grosssum / (i1 - i0), nleg, nmiss, nwb, nfb, 1.0 if np.isnan(eq) or np.isnan(fees) else 0.0])
    return daily, stats, TR[:nrec]


def w_arrays(sel_m, K, bounds):
    """Per-slot WL/WS arrays aligned with slot_arrays() (same slot assignment logic)."""
    WLa = np.full((K, NB), np.nan); WSa = np.full((K, NB), np.nan)
    prev = {}
    for m, t in enumerate(FORM_DATES):
        i0 = GRID.get_loc(t)
        i1 = GRID.get_loc(FORM_DATES[m + 1]) if m + 1 < len(FORM_DATES) else NB
        pairs = sel_m[t][:K]
        slots = {}
        free = list(range(K))
        for (a, b, be) in pairs:
            if (a, b) in prev:
                slots[(a, b)] = prev[(a, b)]; free.remove(prev[(a, b)])
        for (a, b, be) in pairs:
            if (a, b) not in slots:
                slots[(a, b)] = free.pop(0)
        for (a, b, be) in pairs:
            key = (t, a, b, round(float(be), 10))
            if key in bounds:
                wl, ws = bounds[key]
                n = min(len(wl), i1 - i0)
                WLa[slots[(a, b)], i0:i0 + n] = wl[:n]; WSa[slots[(a, b)], i0:i0 + n] = ws[:n]
        prev = slots
    return WLa, WSa


def all_pair_months(sel):
    out = set()
    for key, d in sel.items():
        for t, v in d.items():
            for (a, b, be) in v:
                out.add((pd.Timestamp(t), a, b, round(float(be), 10)))
    return out


def run_grid2(P, U, sel, bounds, mmr, imr, schemes, sig_grid, levs, margins, periods, execs, liqs, tag,
              fee=FEE_T, slip_mult=1.0, log=print):
    O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
    Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
    FR = P['fr'].astype(np.float64)
    day_all = GRID.normalize()
    rows = []
    t_start = time.time()
    for (method, hedge, Wf, K) in schemes:
        WLa, WSa = w_arrays(sel[(method, hedge, Wf)], K, bounds) if bounds is not None else (np.full((K, NB), np.nan),) * 2
        for Wz in sorted(set(s[0] for s in sig_grid)):
            PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, sel[(method, hedge, Wf)], K, Wz, base_slip)
            SA = SA * slip_mult; SB = SB * slip_mult
            for (wz, zin, zout, zstop) in [s for s in sig_grid if s[0] == Wz]:
                for pname, (p0, p1) in periods.items():
                    i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
                    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
                    DAY = ((day_all - p0).days).values.astype(np.int64)
                    for ex in execs:
                        for liq in liqs:
                            for margin in margins:
                                for lev in levs:
                                    if margin == 'sleeve' and lev < 5:
                                        continue
                                    if liq == 'm1' and lev < 5 and 'h1' in liqs:
                                        continue      # no liquidation possible below 5x: identical to h1 except DD
                                    daily, st, _ = sim2(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z,
                                                        SA, SB, NEWM, WLa, WSa, mmr, imr, zin, zout, zstop, Wz,
                                                        float(lev), margin == 'sleeve', fee, FEE_M * (fee > 0),
                                                        RANGE_SLIP * slip_mult, ex == 'maker', liq == 'm1', 1)
                                    m = metrics(daily, days)
                                    rows.append(dict(method=method, hedge=hedge, Wf=Wf, K=K, Wz=Wz, zin=zin, zout=zout,
                                                     zstop=zstop, exec=ex, liq=liq, margin=margin, lev=lev,
                                                     period=pname, cagr=m['cagr'], final=m['final'],
                                                     sharpe=m['sharpe'], worst_day=m['worst_day'],
                                                     maxdd_intrabar=st[1], trades=int(st[2]), liqs=int(st[3]),
                                                     stops=int(st[4]), timeouts=int(st[5]), forced=int(st[6]),
                                                     fees=st[7], funding=st[8], exposure=st[9],
                                                     avg_gross_lev=st[10], legged=int(st[11]),
                                                     maker_miss=int(st[12]), wbars=int(st[13]), fbbars=int(st[14]),
                                                     nan_flag=int(st[15]),
                                                     per_year=json.dumps({str(k): round(v, 4)
                                                                          for k, v in m['per_year'].items()})))
        log(f'{tag} {method} {hedge} {Wf} {K} done {len(rows)} {time.time() - t_start:.0f}s')
    return pd.DataFrame(rows)


SCHEMES = [(m, h, w, k) for m in ['coint', 'corr'] for h in ['lvl', 'ret'] for w in [60, 120] for k in [3, 5, 10]]
SCHEMES += [('fixed', h, w, 5) for h in ['lvl', 'ret'] for w in [60, 120]]
SIG = [(Wz, zin, zout, zin + dz) for Wz in [72, 168, 336] for zin in [1.5, 2.0, 2.5, 3.0] for zout in [0.0, 0.5]
       for dz in [1.5, 3.0, 99.0]]

_G = {}


def _worker(scheme):
    g = _G
    return run_grid2(g['P'], g['U'], g['sel'], g['bounds'], g['mmr'], g['imr'], [scheme], SIG, LEVS,
                     ['cross', 'sleeve'], g['periods'], ['taker', 'maker'], ['h1', 'm1'], 'v2',
                     log=lambda s: print(s, flush=True))


if __name__ == '__main__':
    import multiprocessing as mp
    P, syms, first_bar, U, mmr, imr = prepare()
    sel = build_selections(P, U, first_bar)
    t = time.time()
    bounds = minute_bound.compute_bounds(sorted(all_pair_months(sel)), list(P['syms']), P['o'].astype(np.float64),
                                         GRID, FORM_DATES, log=lambda s: print(s, flush=True))
    print('bounds', len(bounds), f'{time.time() - t:.0f}s', flush=True)
    # keep the bounds in memory only for this run, plus a compact float16 copy for re-use (small)
    _G.update(P=P, U=U, sel=sel, bounds=bounds, mmr=mmr, imr=imr, periods={'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)})
    with mp.get_context('fork').Pool(4) as pool:
        parts = pool.map(_worker, SCHEMES, chunksize=1)
    df = pd.concat(parts, ignore_index=True)
    df.to_csv(os.path.join(OUT, 'grid_v2.csv.gz'), index=False, compression='gzip')
    print(df.shape)
