"""VERIFIER COPY with optional less-pessimistic rules (opt bitmask: 1 = maker fill on touch instead of one-tick
trade-through; 2 = a stop that lies between the open and the liquidation price is taken before liquidation in the same bar;
4 = when stop and take-profit are both touched in one bar, the one nearer the open is taken first; 8 = ORB entry bar is
stopped only if the bar CLOSES beyond the stop). opt=0 reproduces kernels.py exactly.
Numba simulators (one coin sleeve, one window) for the three strategies.

Conventions shared by all kernels
- 1m bars (last-price OHLC for fills, stops, take-profits, marks; mark-price high/low for liquidation).
- Leverage L = notional at entry / sleeve equity at entry. Isolated margin = the whole sleeve equity.
- Liquidation: when equity at the mark-price extreme <= (tier-1 MMR + taker fee) * notional at mark.
  Checked at the intrabar extreme; if a stop and the liquidation price are both touched in the same bar the bar is
  scored as a LIQUIDATION (conservative). Liquidation => sleeve equity 0 (margin lost); the sleeve stays dead until the
  next month start, when the monthly rebalance refills it with fresh capital (equity reset to 1 in sleeve units).
- Taker orders: fee FEE_T on notional, slippage = base_slip + RANGE_SLIP * (high-low)/open of the execution bar.
  Stop orders are stop-market (taker), filled at the stop or at the open if the bar gapped through it.
- Maker (resting limit) orders fill at their limit price only if the bar trades THROUGH it by at least one OKX tick
  (buy: low <= limit - tick; sell: high >= limit + tick). Fee FEE_M. Unfilled orders simply do not trade.
- Same-bar ordering is always resolved against the strategy: stop before take-profit; a limit order that fills
  inside a bar may be stopped in that same bar but cannot take profit in it.
- Output per day d: r[d] = close_equity/ref - 1, tr[d] = trough_equity/ref, ref = previous day close equity
  (or 1 after a revival). A liquidation day has r = -1.
"""
import numpy as np
from numba import njit


@njit(cache=True)
def _liq_price(d, Q, P0, Ea, mmr, fee_t):
    if d == 1:
        return (Q * P0 - Ea) / (Q * (1.0 - mmr - fee_t))
    return (Ea + Q * P0) / (Q * (1.0 + mmr + fee_t))


# ============================================================================ (i) wick / cascade reversal
@njit(cache=True)
def wick_sim(o, h, l, c, mh, ml, mc, fund, fflag, day, month, i0, i1,
             ev_idx, ev_dir, ev_mf,
             tick, mmr, bslip, L, entry_lim, f_tp, s_stop, H, long_only, cooldown, lim_k, lim_valid,
             fee_t, fee_m, rslip, cap_frac, out_r, out_tr, stats, trades, opt):
    thr = 0.0 if (opt & 1) else tick
    d0 = day[i0]
    E = 1.0
    pos = 0; Q = 0.0; P0 = 0.0; Ea = 0.0; stop = 0.0; tp = 0.0; mliq = 0.0; ent_i = 0; Epre = 1.0
    pend = 0; pend_px = 0.0; pend_exp = 0; pend_mf = 0.0
    cool_until = 0
    dead = False; dead_month = -1
    cur_d = -1; ref = 1.0; trough = 1.0; last = 1.0
    nev = ev_idx.shape[0]
    k = 0
    while k < nev and ev_idx[k] < i0 - 1:
        k += 1
    ntr = 0
    for i in range(i0, i1):
        dd = day[i] - d0
        if dd != cur_d:
            if cur_d >= 0:
                if ref > 0:
                    out_r[cur_d] = last / ref - 1.0; out_tr[cur_d] = trough / ref
                else:
                    out_r[cur_d] = 0.0; out_tr[cur_d] = 1.0
            cur_d = dd
            if dead and month[i] != dead_month:
                dead = False; E = 1.0; last = 1.0; pos = 0; pend = 0; cool_until = 0
            ref = 0.0 if dead else last
            trough = last
        if dead:
            continue
        # advance event pointer: the usable event is the one at bar close i-1
        while k < nev and ev_idx[k] < i - 1:
            k += 1
        slip = bslip + rslip * (h[i] - l[i]) / o[i]
        # 1. funding at bar open
        if pos != 0 and fflag[i] == 1:
            Ea -= pos * fund[i] * Q * mc[i - 1]
            mliq = _liq_price(pos, Q, P0, Ea, mmr, fee_t)
        bar_trough = 1e300
        exited = False; liq = False
        # 2. time exit at the open
        if pos != 0 and i - ent_i >= H:
            px = o[i] * (1.0 - pos * slip)
            E = Ea + pos * Q * (px - P0) - fee_t * Q * px
            stats[4] += 1; exited = True
        # 3. entries at the open (market) / limit placement
        if pos == 0 and (not exited) and pend == 0 and i >= cool_until and k < nev and ev_idx[k] == i - 1:
            dr = ev_dir[k]
            if not (long_only and dr == -1):
                mf = ev_mf[k]
                if entry_lim:
                    pend = dr; pend_px = c[i - 1] * (1.0 - dr * lim_k * mf); pend_exp = i + lim_valid; pend_mf = mf
                    stats[5] += 1
                else:
                    P0 = o[i] * (1.0 + dr * slip)
                    Q = L * E / P0; Ea = E - fee_t * L * E; Epre = E
                    pos = dr; ent_i = i
                    mliq = _liq_price(pos, Q, P0, Ea, mmr, fee_t)
                    liqd = (P0 - mliq) / P0 if pos == 1 else (mliq - P0) / P0
                    if liqd <= 0.0 or mliq <= 0.0:
                        liqd = 1.0
                    sd = s_stop * mf
                    if sd > cap_frac * liqd:
                        sd = cap_frac * liqd
                    stop = P0 * (1.0 - pos * sd); tp = P0 * (1.0 + pos * f_tp * mf)
        # 4. limit fill (maker) - only if flat with a pending order
        fill_bar = False
        if pos == 0 and pend != 0 and (not exited):
            if i >= pend_exp:
                pend = 0
            else:
                hit = (l[i] <= pend_px - thr) if pend == 1 else (h[i] >= pend_px + thr)
                if hit:
                    P0 = pend_px; Q = L * E / P0; Ea = E - fee_m * L * E; Epre = E
                    pos = pend; ent_i = i; pend = 0; fill_bar = True; stats[6] += 1
                    mliq = _liq_price(pos, Q, P0, Ea, mmr, fee_t)
                    liqd = (P0 - mliq) / P0 if pos == 1 else (mliq - P0) / P0
                    if liqd <= 0.0 or mliq <= 0.0:
                        liqd = 1.0
                    sd = s_stop * pend_mf
                    if sd > cap_frac * liqd:
                        sd = cap_frac * liqd
                    stop = P0 * (1.0 - pos * sd); tp = P0 * (1.0 + pos * f_tp * pend_mf)
        # 5. intrabar liquidation / stop / take-profit
        if pos != 0 and not exited:
            if pos == 1:
                liq_hit = ml[i] <= mliq
                stop_hit = l[i] <= stop
                tp_hit = (not fill_bar) and h[i] >= tp + thr
                adverse = min(l[i], ml[i])
            else:
                liq_hit = mh[i] >= mliq
                stop_hit = h[i] >= stop
                tp_hit = (not fill_bar) and l[i] <= tp - thr
                adverse = max(h[i], mh[i])
            if (opt & 2) and liq_hit and stop_hit:
                if fill_bar or (pos == 1 and o[i] > mliq) or (pos == -1 and o[i] < mliq):
                    liq_hit = False
            if (opt & 4) and stop_hit and tp_hit:
                if (pos == 1 and (tp - o[i]) < (o[i] - stop)) or (pos == -1 and (o[i] - tp) < (stop - o[i])):
                    stop_hit = False
            if liq_hit:
                liq = True
            elif stop_hit:
                if fill_bar:
                    sp = stop
                else:
                    sp = min(o[i], stop) if pos == 1 else max(o[i], stop)
                px = sp * (1.0 - pos * slip)
                E = Ea + pos * Q * (px - P0) - fee_t * Q * px
                stats[2] += 1; exited = True
            else:
                bar_trough = Ea + pos * Q * (adverse - P0)
                if tp_hit:
                    px = tp
                    E = Ea + pos * Q * (px - P0) - fee_m * Q * px
                    stats[3] += 1; exited = True
        if liq:
            stats[1] += 1; stats[0] += 1
            if ntr < trades.shape[0]:
                trades[ntr] = -1.0
            ntr += 1
            E = 0.0; pos = 0; dead = True; dead_month = month[i]; last = 0.0; trough = 0.0
            continue
        if exited:
            if E < 0.0:
                E = 0.0
            stats[0] += 1
            if ntr < trades.shape[0]:
                trades[ntr] = E / Epre - 1.0
            ntr += 1
            pos = 0; cool_until = i + cooldown
            if E <= 1e-12:
                dead = True; dead_month = month[i]; last = 0.0; trough = 0.0
                continue
        # 6. mark to market at the close
        if pos != 0:
            eq = Ea + pos * Q * (c[i] - P0)
            if i == i1 - 1:   # close at the window end (taker)
                px = c[i] * (1.0 - pos * slip)
                eq = Ea + pos * Q * (px - P0) - fee_t * Q * px
        else:
            eq = E
        if bar_trough < trough:
            trough = bar_trough
        if eq < trough:
            trough = eq
        last = eq
    if cur_d >= 0:
        if ref > 0:
            out_r[cur_d] = last / ref - 1.0; out_tr[cur_d] = trough / ref
        else:
            out_r[cur_d] = 0.0; out_tr[cur_d] = 1.0
    return ntr


# ============================================================================ (ii) grid
@njit(cache=True)
def grid_sim(o, h, l, c, mh, ml, mc, fund, fflag, day, month, i0, i1,
             tick, mmr, bslip, L, g, N, W_hours, brk_stop,
             fee_t, fee_m, rslip, out_r, out_tr, stats, opt):
    thr = 0.0 if (opt & 1) else tick
    """Neutral futures grid. Levels lvl_j = A*(1 - j*g), j = -N..N (A = anchor). Holding q units (q>0 long, q<0 short,
    |q|<=N, one unit = u coins, u = L*equity/(N*A) set whenever the grid is flat). Resting orders at bar start:
    buys at lvl_{q+1..N}, sells at lvl_{q-1..-N}; counter-orders created by a fill only rest from the next bar.
    W_hours = 0: fixed anchor (set at start / restart). W_hours > 0: anchor = EMA of hourly closes (span W_hours),
    updated each hour; if the price is then outside the hysteresis band of the new ladder the inventory is brought to
    the ladder's target with a taker order. brk_stop: a trade through lvl_{+-(N+1)} closes the whole inventory
    (stop-market) and the grid restarts at the next bar open centred on that price; otherwise the inventory is held.
    stats: 0 maker fills, 1 liquidations, 2 stops, 3 taker rebalance trades, 4 restarts."""
    d0 = day[i0]
    C = 1.0; q = 0; u = 0.0; A = o[i0]; ema = o[i0]
    alpha = 2.0 / (W_hours + 1.0) if W_hours > 0 else 0.0
    restart = True
    dead = False; dead_month = -1
    cur_d = -1; ref = 1.0; trough = 1.0; last = 1.0
    for i in range(i0, i1):
        dd = day[i] - d0
        if dd != cur_d:
            if cur_d >= 0:
                if ref > 0:
                    out_r[cur_d] = last / ref - 1.0; out_tr[cur_d] = trough / ref
                else:
                    out_r[cur_d] = 0.0; out_tr[cur_d] = 1.0
            cur_d = dd
            if dead and month[i] != dead_month:
                dead = False; C = 1.0; q = 0; last = 1.0; restart = True
            ref = 0.0 if dead else last
            trough = last
        if dead:
            continue
        slip = bslip + rslip * (h[i] - l[i]) / o[i]
        if restart:
            A = o[i]; ema = o[i]; q = 0; u = L * C / (N * A); restart = False; stats[4] += 1
        # funding
        if q != 0 and fflag[i] == 1:
            C -= q * u * mc[i - 1] * fund[i]
        stopped = False
        # hourly anchor update (EMA mode)
        if W_hours > 0 and i % 60 == 0 and i > i0:
            ema += alpha * (c[i - 1] - ema)
            A = ema
            p = o[i]
            if brk_stop and (p <= A * (1.0 - (N + 1) * g) or p >= A * (1.0 + (N + 1) * g)):
                if q != 0:
                    px = p * (1.0 - slip) if q > 0 else p * (1.0 + slip)
                    C += q * u * px - fee_t * abs(q) * u * px
                    stats[2] += 1
                q = 0; stopped = True
            else:
                if q < N and p <= A * (1.0 - (q + 1) * g):
                    qn = int(np.floor((A - p) / (g * A)))
                    if qn > N:
                        qn = N
                    n = qn - q
                    px = p * (1.0 + slip)
                    C -= n * u * px + fee_t * n * u * px
                    q = qn; stats[3] += 1
                elif q > -N and p >= A * (1.0 - (q - 1) * g):
                    qn = int(np.ceil((A - p) / (g * A)))
                    if qn < -N:
                        qn = -N
                    n = q - qn
                    px = p * (1.0 - slip)
                    C += n * u * px - fee_t * n * u * px
                    q = qn; stats[3] += 1
        if stopped:
            restart = True
            eq = C
            if eq < trough:
                trough = eq
            last = eq
            continue
        q0 = q
        # maker fills from the ladder resting at bar start
        jmax = int(np.floor((A - l[i] - thr) / (g * A) + 1e-9))      # buys at lvl_j fill for j <= jmax
        if jmax > N:
            jmax = N
        nb = jmax - q0 if jmax > q0 else 0
        jmin = int(np.ceil((A - h[i] + thr) / (g * A) - 1e-9))       # sells at lvl_j fill for j >= jmin
        if jmin < -N:
            jmin = -N
        ns = q0 - jmin if jmin < q0 else 0
        B = 0.0
        if nb > 0:
            sj = (q0 + 1 + q0 + nb) * nb / 2.0
            B = A * (nb - g * sj) * u
        S = 0.0
        if ns > 0:
            sj = (q0 - 1 + q0 - ns) * ns / 2.0
            S = A * (ns - g * sj) * u
        C_low = C - B * (1.0 + fee_m); q_low = q0 + nb
        C_high = C + S * (1.0 - fee_m); q_high = q0 - ns
        liq_lo = False; liq_hi = False
        if q_low > 0:
            if C_low + q_low * u * ml[i] <= (mmr + fee_t) * q_low * u * ml[i]:
                liq_lo = True
        if q_high < 0:
            if C_high + q_high * u * mh[i] <= (mmr + fee_t) * (-q_high) * u * mh[i]:
                liq_hi = True
        if (opt & 2) and brk_stop:
            if liq_lo and l[i] <= A * (1.0 - (N + 1) * g):
                sp = min(o[i], A * (1.0 - (N + 1) * g))
                if C_low + q_low * u * sp > (mmr + fee_t) * q_low * u * sp:
                    liq_lo = False
            if liq_hi and h[i] >= A * (1.0 + (N + 1) * g):
                sp = max(o[i], A * (1.0 + (N + 1) * g))
                if C_high + q_high * u * sp > (mmr + fee_t) * (-q_high) * u * sp:
                    liq_hi = False
        liq = liq_lo or liq_hi
        t_low = C_low + q_low * u * min(l[i], ml[i])
        t_high = C_high + q_high * u * max(h[i], mh[i])
        bt = min(t_low, t_high)
        if liq:
            stats[1] += 1
            C = 0.0; q = 0; dead = True; dead_month = month[i]; last = 0.0; trough = 0.0
            continue
        stop_low = brk_stop and l[i] <= A * (1.0 - (N + 1) * g)
        stop_high = brk_stop and h[i] >= A * (1.0 + (N + 1) * g)
        if stop_low or stop_high:
            e1 = 1e300; e2 = 1e300
            if stop_low:     # down-first path: all buys filled, then the whole inventory stopped out
                sp = min(o[i], A * (1.0 - (N + 1) * g))
                px = sp * (1.0 - slip)
                e1 = C_low + q_low * u * px - fee_t * abs(q_low) * u * px
            if stop_high:    # up-first path
                sp = max(o[i], A * (1.0 + (N + 1) * g))
                px = sp * (1.0 + slip)
                e2 = C_high + q_high * u * px - fee_t * abs(q_high) * u * px
            C = min(e1, e2); q = 0; stats[2] += 1; restart = True
            stats[0] += nb + ns
            if C <= 1e-12:
                C = 0.0; dead = True; dead_month = month[i]; last = 0.0; trough = 0.0
                continue
            if bt < trough:
                trough = bt
            if C < trough:
                trough = C
            last = C
            continue
        C = C - B * (1.0 + fee_m) + S * (1.0 - fee_m)
        q = q0 + nb - ns
        stats[0] += nb + ns
        eq = C + q * u * c[i]
        if i == i1 - 1 and q != 0:
            px = c[i] * (1.0 - slip) if q > 0 else c[i] * (1.0 + slip)
            eq = C + q * u * px - fee_t * abs(q) * u * px
        if q == 0:
            u = L * C / (N * A)
        if bt < trough:
            trough = bt
        if eq < trough:
            trough = eq
        last = eq
    if cur_d >= 0:
        if ref > 0:
            out_r[cur_d] = last / ref - 1.0; out_tr[cur_d] = trough / ref
        else:
            out_r[cur_d] = 0.0; out_tr[cur_d] = 1.0
    return 0


# ============================================================================ (iii) opening-range breakout
@njit(cache=True)
def orb_sim(o, h, l, c, mh, ml, mc, fund, fflag, day, month, i0, i1,
            s_arm, s_until, s_exit, s_orh, s_orl, s_ok,
            tick, mmr, bslip, L, stop_mid, k_tp,
            fee_t, fee_m, rslip, cap_frac, out_r, out_tr, stats, trades, opt):
    thr = 0.0 if (opt & 1) else tick
    """One breakout trade per session: stop-market entry at ORH+tick / ORL-tick between s_arm and s_until, stop at the
    opposite side ('opp') or mid of the range, optional maker take-profit k_tp * range width, forced taker exit at
    s_exit. stats: 0 trades, 1 liq, 2 stops, 3 tp, 4 time exits."""
    d0 = day[i0]
    E = 1.0
    pos = 0; Q = 0.0; P0 = 0.0; Ea = 0.0; stop = 0.0; tp = 0.0; mliq = 0.0; Epre = 1.0; exit_at = 0
    armed = False; a_until = 0; orh = 0.0; orl = 0.0
    dead = False; dead_month = -1
    cur_d = -1; ref = 1.0; trough = 1.0; last = 1.0
    ns = s_arm.shape[0]
    k = 0
    while k < ns and s_arm[k] < i0:
        k += 1
    ntr = 0
    for i in range(i0, i1):
        dd = day[i] - d0
        if dd != cur_d:
            if cur_d >= 0:
                if ref > 0:
                    out_r[cur_d] = last / ref - 1.0; out_tr[cur_d] = trough / ref
                else:
                    out_r[cur_d] = 0.0; out_tr[cur_d] = 1.0
            cur_d = dd
            if dead and month[i] != dead_month:
                dead = False; E = 1.0; last = 1.0; pos = 0; armed = False
            ref = 0.0 if dead else last
            trough = last
        if dead:
            while k < ns and s_arm[k] <= i:
                k += 1
            continue
        slip = bslip + rslip * (h[i] - l[i]) / o[i]
        if k < ns and s_arm[k] == i:
            if pos == 0 and s_ok[k] == 1:
                armed = True; a_until = s_until[k]; orh = s_orh[k]; orl = s_orl[k]; exit_at = s_exit[k]
            else:
                armed = False
            k += 1
        if pos != 0 and fflag[i] == 1:
            Ea -= pos * fund[i] * Q * mc[i - 1]
            mliq = _liq_price(pos, Q, P0, Ea, mmr, fee_t)
        exited = False; liq = False; bar_trough = 1e300; entry_bar = False
        if pos != 0 and i >= exit_at:
            px = o[i] * (1.0 - pos * slip)
            E = Ea + pos * Q * (px - P0) - fee_t * Q * px
            stats[4] += 1; exited = True
        if armed and pos == 0 and not exited:
            if i >= a_until:
                armed = False
            else:
                up = h[i] >= orh + tick
                dn = l[i] <= orl - tick
                dr = 0
                if up and dn:
                    dr = 1 if (orh - o[i]) <= (o[i] - orl) else -1
                elif up:
                    dr = 1
                elif dn:
                    dr = -1
                if dr != 0:
                    if dr == 1:
                        P0 = max(o[i], orh + tick) * (1.0 + slip)
                    else:
                        P0 = min(o[i], orl - tick) * (1.0 - slip)
                    Q = L * E / P0; Ea = E - fee_t * L * E; Epre = E
                    pos = dr; armed = False; entry_bar = True
                    mliq = _liq_price(pos, Q, P0, Ea, mmr, fee_t)
                    liqd = (P0 - mliq) / P0 if pos == 1 else (mliq - P0) / P0
                    if liqd <= 0.0 or mliq <= 0.0:
                        liqd = 1.0
                    ref_stop = 0.5 * (orh + orl) if stop_mid else (orl if pos == 1 else orh)
                    sd = abs(P0 - ref_stop) / P0
                    if sd > cap_frac * liqd:
                        sd = cap_frac * liqd
                    if sd < 2.0 * tick / P0:
                        sd = 2.0 * tick / P0
                    stop = P0 * (1.0 - pos * sd)
                    tp = P0 + pos * k_tp * (orh - orl) if k_tp > 0 else (1e300 if pos == 1 else -1.0)
        if pos != 0 and not exited:
            if pos == 1:
                liq_hit = ml[i] <= mliq
                stop_hit = l[i] <= stop
                tp_hit = (not entry_bar) and h[i] >= tp + thr
                adverse = min(l[i], ml[i])
                if (opt & 8) and entry_bar:
                    stop_hit = c[i] <= stop
            else:
                liq_hit = mh[i] >= mliq
                stop_hit = h[i] >= stop
                tp_hit = (not entry_bar) and l[i] <= tp - thr
                adverse = max(h[i], mh[i])
                if (opt & 8) and entry_bar:
                    stop_hit = c[i] >= stop
            if (opt & 2) and liq_hit and stop_hit:
                if entry_bar or (pos == 1 and o[i] > mliq) or (pos == -1 and o[i] < mliq):
                    liq_hit = False
            if (opt & 4) and stop_hit and tp_hit:
                if (pos == 1 and (tp - o[i]) < (o[i] - stop)) or (pos == -1 and (o[i] - tp) < (stop - o[i])):
                    stop_hit = False
            if liq_hit:
                liq = True
            elif stop_hit:
                if entry_bar:
                    sp = stop
                else:
                    sp = min(o[i], stop) if pos == 1 else max(o[i], stop)
                px = sp * (1.0 - pos * slip)
                E = Ea + pos * Q * (px - P0) - fee_t * Q * px
                stats[2] += 1; exited = True
            else:
                bar_trough = Ea + pos * Q * (adverse - P0)
                if tp_hit:
                    px = tp
                    E = Ea + pos * Q * (px - P0) - fee_m * Q * px
                    stats[3] += 1; exited = True
        if liq:
            stats[1] += 1; stats[0] += 1
            if ntr < trades.shape[0]:
                trades[ntr] = -1.0
            ntr += 1
            E = 0.0; pos = 0; dead = True; dead_month = month[i]; last = 0.0; trough = 0.0; armed = False
            continue
        if exited:
            if E < 0.0:
                E = 0.0
            stats[0] += 1
            if ntr < trades.shape[0]:
                trades[ntr] = E / Epre - 1.0
            ntr += 1
            pos = 0
            if E <= 1e-12:
                dead = True; dead_month = month[i]; last = 0.0; trough = 0.0; armed = False
                continue
        if pos != 0:
            eq = Ea + pos * Q * (c[i] - P0)
            if i == i1 - 1:
                px = c[i] * (1.0 - pos * slip)
                eq = Ea + pos * Q * (px - P0) - fee_t * Q * px
        else:
            eq = E
        if bar_trough < trough:
            trough = bar_trough
        if eq < trough:
            trough = eq
        last = eq
    if cur_d >= 0:
        if ref > 0:
            out_r[cur_d] = last / ref - 1.0; out_tr[cur_d] = trough / ref
        else:
            out_r[cur_d] = 0.0; out_tr[cur_d] = 1.0
    return ntr
