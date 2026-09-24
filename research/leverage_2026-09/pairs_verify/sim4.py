import numpy as np
from numba import njit


@njit(cache=True)
def _close_leg_px(q, px, sl):
    return px * (1.0 - sl) if q > 0 else px * (1.0 + sl)


@njit(cache=True)
def sim4(O, H, L, C, MH, ML, FR, DAY, i0, i1, nd, PA, PB, BE, Z, SA, SB, NEWM, WL, WS, mmr, imr,
         zin, zout, zstop, maxhold, lev, sleeve, fee, feem, rslip, maker, use_w, rec, tickv, liqmode):
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
                    fillA = H[a, i] >= lpa[k] * (1.0 + tickv[a])
                else:
                    fillA = L[a, i] <= lpa[k] * (1.0 - tickv[a])
                if qB[k] > 0:
                    fillB = H[b, i] >= lpb[k] * (1.0 + tickv[b])
                else:
                    fillB = L[b, i] <= lpb[k] * (1.0 - tickv[b])
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
                    fillA = L[a, i] <= lpa[k] * (1.0 - tickv[a]); fillB = H[b, i] >= lpb[k] * (1.0 + tickv[b])
                else:
                    fillA = H[a, i] >= lpa[k] * (1.0 + tickv[a]); fillB = L[b, i] <= lpb[k] * (1.0 - tickv[b])
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
        ew_c = cash[0]; mm_c = 0.0; gn_c = 0.0
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
                ew_c += upl_w; mm_c += mm; gn_c += abs(qA[k]) * ha + abs(qB[k]) * hb
        if not sleeve:
            if anypos and ew_c <= mm_c:
                nliq += 1
                if nrec < rec:
                    TR[nrec, 0] = -1; TR[nrec, 4] = i; TR[nrec, 5] = -1.0; TR[nrec, 6] = 5
                    nrec += 1
                if liqmode == 1:
                    resid = ew_c - fee * gn_c
                    if resid < 0.0:
                        resid = 0.0
                    cash[0] = resid
                    if resid <= 0.0:
                        dead = True
                    for k in range(K):
                        pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1; blocked[k] = 1
                    ew_c = resid
                else:
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


