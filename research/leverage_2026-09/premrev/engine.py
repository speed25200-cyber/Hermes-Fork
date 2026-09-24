"""Intraday perp-premium mean reversion (delta neutral perp vs spot), 1m bars, Binance data as OKX proxy.

Signal (known at the close of minute t):
    b_t   = F_close/S_close - 1           tradeable basis, Binance USD-M perp vs Binance spot last prices
    med_t = median(b_{t-W..t-1})          trailing baseline (past only)
    d_t   = b_t - med_t
    side +1 (short perp / long spot)  if d_t >  k
    side -1 (long perp / short spot)  if d_t < -k   (only when both sides enabled; spot short = borrowed coin)
    optional confirmation: Binance premium index (impact prices vs index) deviation from its own trailing
    median has the same sign and exceeds k/2.
Exit: first close u >= entry with side*d_u <= x*k, or after H minutes; then executed like the entry.

Execution models (mode)
    0 taker, lat=0: both legs at the OPEN of the next minute (first trade after the signal close), taker fee,
                    slippage per leg = base + kappa * (high-low)/close of that leg in the execution minute.
    0 taker, lat=1: both legs at the CLOSE of the next minute (one minute of latency), same costs.
    1 maker (leg-in): at the signal close, post the perp limit at the perp close and the spot limit at the
                    spot close. A limit fills only if the market trades THROUGH it by >= buf (1 bp) in a later
                    minute (sell: high >= limit*(1+buf); buy: low <= limit*(1-buf)). The first leg to trade
                    through (minute u) fills at its limit (maker fee, no slippage); the other leg is hedged at
                    once as taker (fee + slippage) at the prevailing synchronous basis of minute u, estimated as
                    (b_open(u) + b_close(u))/2 - so a stale closing print cannot be locked in. If both legs trade
                    through in u (order unknown) fees/slippage are averaged over the two orders. No fill within
                    T_fill minutes: entry signal lost (opportunity cost); exit: both legs crossed as taker at the
                    close of the timeout minute.
    3 maker upper bound (optimistic, for reference only): legs that trade through in the first fill minute
                    fill at their limits (both at the stale closes if both trade through); the other leg is
                    hedged as taker at (open+close)/2 of that minute (hedge_mid=1) or its close (hedge_mid=0).

Accounting per trade, per unit of spot notional N0 = Q*S0 (equal coin quantity Q on both legs):
    gross = side*[(F0-F1) + (S1-S0)]/S0, fees & slippage on both legs, funding (Binance events while held;
    the short perp receives), borrow interest (USDT for long spot, coin for short spot; min 1 hour).

Leverage L := notional of EACH leg / account equity at entry. Three margin models, checked every minute of
the holding window (intrabar):
  mc   OKX multi-currency cross margin (published tier-1 parameters): perp at mark, spot at index; adjusted
       equity = equity - haircut*spot collateral; maintenance = perp MMR*notional + borrow MMR*liability.
       Opening requires adjusted equity >= perp IMR*notional + 10%*liability (else the order is REJECTED).
  pm   idealised portfolio margin (ASSUMPTION, not published OKX numbers): maintenance = m_pm*notional,
       opening requires 1.3*m_pm*notional.
  sep  isolated perp + isolated spot-margin account; spot margin gets max(half the equity, 10% of notional
       [OKX isolated spot-margin max 10x]), the perp the rest (rejected if below perp IMR). Perp leg
       liquidated at the mark high/low, spot leg at the Binance spot low/high (conservative vs index); the
       liquidated leg's margin is lost, the surviving leg is closed at that minute's close as taker.
  Intrabar mark premium (mc, pm): stress 0 = worse of the two adjacent 1m closes of the Binance mark premium
  (the mark is a smoothed price); stress 1 = mark premium close + premium-index intrabar excursion.
  Liquidation (mc, pm): the position is closed at the worst point, losing penalty + MMR + taker fee on the
  notional; the account continues with what is left.
"""
import numpy as np
from numba import njit

LEVS = np.array([1.0, 3.0, 5.0, 10.0, 15.0, 20.0])
NL = len(LEVS)
MODES = ['mc', 'pm', 'sep']
FIELDS = ['ret', 'worst', 'liq', 'rej']
C_SIG, C_E, C_X, C_SIDE, C_GROSS, C_FEES, C_SLIP, C_FUND, C_HOURS, C_MKIN, C_MKOUT, C_B0, C_DEV = range(13)
NBASE = 13
NCOL = NBASE + len(MODES) * len(FIELDS) * NL


def col(mode, field):
    """first column of the NL-block for (margin mode, field)"""
    return NBASE + (MODES.index(mode) * len(FIELDS) + FIELDS.index(field)) * NL


@njit(cache=True)
def _slip(base, kappa, rng):
    r = rng if rng == rng else 0.0
    return base + kappa * max(r, 0.0)


@njit(cache=True)
def _through(mode, buf, rng):
    if mode == 2:
        return max(buf, 0.2 * max(rng, 0.0))
    return buf


@njit(cache=True)
def _hedge_px_s(mid, u, Sc, so):
    if mid:
        return 0.5 * (Sc[u] * (1.0 + so[u]) + Sc[u])
    return Sc[u]


@njit(cache=True)
def _hedge_px_f(mid, u, Sc, so, bo, bc):
    fc = Sc[u] * (1.0 + bc[u])
    if mid:
        return 0.5 * (Sc[u] * (1.0 + so[u]) * (1.0 + bo[u]) + fc)
    return fc


@njit(cache=True)
def gen_trades(t_start, t_end, ok, bc, bo, Sc, so, sh, sl, fh, fl, mpc, ph, pl, mh, ml, ic, fund, rusdt,
               med, pdev, k, x_exit, H, both, confirm, mode, lat, T_fill, buf,
               fs_t, fp_t, fs_m, fp_m, slip_base, kappa,
               haircut, mm, im, lp, bmmr, cbmmr, mmr_m, m_pm, r_coin, levs, stress, hedge_mid):
    n = len(bc)
    NLv = len(levs)
    out = np.zeros((20000, 13 + 12 * NLv))
    nt = 0
    t = t_start
    while t < t_end - 2:
        if not ok[t] or med[t] != med[t]:
            t += 1
            continue
        d = bc[t] - med[t]
        side = 0
        if d > k:
            side = 1
        elif both and d < -k:
            side = -1
        if side != 0 and confirm:
            if not (side * pdev[t] > 0.5 * k):
                side = 0
        if side == 0:
            t += 1
            continue
        # ---------------- entry ----------------
        mk_in = 0.0
        if mode == 0:
            e = t + 1
            if lat == 0:
                S0 = Sc[e] * (1.0 + so[e]); F0 = S0 * (1.0 + bo[e])
            else:
                S0 = Sc[e]; F0 = Sc[e] * (1.0 + bc[e])
            fee_p_in = fp_t * F0 / S0; fee_s_in = fs_t
            slp_p_in = _slip(slip_base, kappa, fh[e] - fl[e]) * F0 / S0
            slp_s_in = _slip(slip_base, kappa, sh[e] - sl[e])
            e_open = (lat == 0)
        else:
            Fl = Sc[t] * (1.0 + bc[t]); Sl = Sc[t]
            fu_p = -1; fu_s = -1; fu = -1
            for u in range(t + 1, min(t + 1 + T_fill, n)):
                Fu = Sc[u] * (1.0 + bc[u])
                bp = _through(mode, buf, fh[u] - fl[u]); bs = _through(mode, buf, sh[u] - sl[u])
                if side == 1 and Fu * (1.0 + fh[u]) >= Fl * (1.0 + bp):
                    fu_p = u
                if side == -1 and Fu * (1.0 + fl[u]) <= Fl * (1.0 - bp):
                    fu_p = u
                if side == 1 and Sc[u] * (1.0 + sl[u]) <= Sl * (1.0 - bs):
                    fu_s = u
                if side == -1 and Sc[u] * (1.0 + sh[u]) >= Sl * (1.0 + bs):
                    fu_s = u
                if fu_p >= 0 or fu_s >= 0:
                    fu = u
                    break
            if fu < 0:
                t = t + T_fill + 1          # nothing filled: signal lost (opportunity cost)
                continue
            # the first fill minute: legs that traded through fill at their limits (maker); the other leg is
            # hedged immediately as taker at the close of that minute
            tt = fu
            if mode == 1:
                # leg-in: the first leg to trade through fills at its limit; the other is hedged at once as
                # taker at the prevailing synchronous basis of that minute, (b_open + b_close)/2
                bsy = 0.5 * (bo[tt] + bc[tt])
                sp_p = _slip(slip_base, kappa, fh[tt] - fl[tt]); sp_s = _slip(slip_base, kappa, sh[tt] - sl[tt])
                if fu_p >= 0 and fu_s >= 0:      # both traded through in the same minute: order unknown -> average
                    F0 = Fl; S0 = Fl / (1.0 + bsy)
                    fee_p = 0.5 * (fp_m + fp_t) * F0; slp_p = 0.5 * sp_p * F0
                    fee_s = 0.5 * (fs_m + fs_t) * S0; slp_s = 0.5 * sp_s * S0
                elif fu_p >= 0:
                    F0 = Fl; S0 = Fl / (1.0 + bsy)
                    fee_p = fp_m * F0; slp_p = 0.0; fee_s = fs_t * S0; slp_s = sp_s * S0
                else:
                    S0 = Sl; F0 = Sl * (1.0 + bsy)
                    fee_s = fs_m * S0; slp_s = 0.0; fee_p = fp_t * F0; slp_p = sp_p * F0
            else:
                if fu_p >= 0:
                    F0 = Fl; fee_p = fp_m * F0; slp_p = 0.0
                else:
                    F0 = _hedge_px_f(hedge_mid, tt, Sc, so, bo, bc); fee_p = fp_t * F0
                    slp_p = _slip(slip_base, kappa, fh[tt] - fl[tt]) * F0
                if fu_s >= 0:
                    S0 = Sl; fee_s = fs_m * S0; slp_s = 0.0
                else:
                    S0 = _hedge_px_s(hedge_mid, tt, Sc, so); fee_s = fs_t * S0
                    slp_s = _slip(slip_base, kappa, sh[tt] - sl[tt]) * S0
            e = tt
            fee_p_in = fee_p / S0; fee_s_in = fee_s / S0; slp_p_in = slp_p / S0; slp_s_in = slp_s / S0
            mk_in = 1.0 if (fu_p >= 0 and fu_s >= 0) else 0.5
            e_open = False
        c_in = fee_p_in + fee_s_in + slp_p_in + slp_s_in
        c_in_s = fee_s_in + slp_s_in
        # ---------------- holding / exit decision ----------------
        u = e
        while True:
            if u >= n - T_fill - 3 or u >= t_end + 2000:
                break
            if ok[u] and med[u] == med[u]:
                if side * (bc[u] - med[u]) <= x_exit * k:
                    break
            if u - e >= H:
                break
            u += 1
        mk_out = 0.0
        if mode == 0:
            xx = u + 1
            if lat == 0:
                S1 = Sc[xx] * (1.0 + so[xx]); F1 = S1 * (1.0 + bo[xx])
            else:
                S1 = Sc[xx]; F1 = Sc[xx] * (1.0 + bc[xx])
            fee_out = fp_t * F1 / S0 + fs_t * S1 / S0
            slp_out = _slip(slip_base, kappa, fh[xx] - fl[xx]) * F1 / S0 + _slip(slip_base, kappa, sh[xx] - sl[xx]) * S1 / S0
            x_open = (lat == 0)
        else:
            Fl = Sc[u] * (1.0 + bc[u]); Sl = Sc[u]
            fu_p = -1; fu_s = -1; fu = -1
            for v in range(u + 1, u + 1 + T_fill):
                Fv = Sc[v] * (1.0 + bc[v])
                bp = _through(mode, buf, fh[v] - fl[v]); bs = _through(mode, buf, sh[v] - sl[v])
                if side == 1 and Fv * (1.0 + fl[v]) <= Fl * (1.0 - bp):
                    fu_p = v
                if side == -1 and Fv * (1.0 + fh[v]) >= Fl * (1.0 + bp):
                    fu_p = v
                if side == 1 and Sc[v] * (1.0 + sh[v]) >= Sl * (1.0 + bs):
                    fu_s = v
                if side == -1 and Sc[v] * (1.0 + sl[v]) <= Sl * (1.0 - bs):
                    fu_s = v
                if fu_p >= 0 or fu_s >= 0:
                    fu = v
                    break
            tt = fu if fu >= 0 else u + T_fill     # no fill by the timeout: both legs crossed as taker at close
            hm = hedge_mid if fu >= 0 else 0
            if mode == 1 and fu >= 0:
                bsy = 0.5 * (bo[tt] + bc[tt])
                sp_p = _slip(slip_base, kappa, fh[tt] - fl[tt]); sp_s = _slip(slip_base, kappa, sh[tt] - sl[tt])
                if fu_p >= 0 and fu_s >= 0:
                    F1 = Fl; S1 = Fl / (1.0 + bsy)
                    fee_p = 0.5 * (fp_m + fp_t) * F1; slp_p = 0.5 * sp_p * F1
                    fee_s = 0.5 * (fs_m + fs_t) * S1; slp_s = 0.5 * sp_s * S1
                elif fu_p >= 0:
                    F1 = Fl; S1 = Fl / (1.0 + bsy)
                    fee_p = fp_m * F1; slp_p = 0.0; fee_s = fs_t * S1; slp_s = sp_s * S1
                else:
                    S1 = Sl; F1 = Sl * (1.0 + bsy)
                    fee_s = fs_m * S1; slp_s = 0.0; fee_p = fp_t * F1; slp_p = sp_p * F1
            else:
                if fu_p >= 0:
                    F1 = Fl; fee_p = fp_m * F1; slp_p = 0.0
                else:
                    F1 = _hedge_px_f(hm, tt, Sc, so, bo, bc); fee_p = fp_t * F1
                    slp_p = _slip(slip_base, kappa, fh[tt] - fl[tt]) * F1
                if fu_s >= 0:
                    S1 = Sl; fee_s = fs_m * S1; slp_s = 0.0
                else:
                    S1 = _hedge_px_s(hm, tt, Sc, so); fee_s = fs_t * S1
                    slp_s = _slip(slip_base, kappa, sh[tt] - sl[tt]) * S1
            xx = tt
            fee_out = (fee_p + fee_s) / S0
            slp_out = (slp_p + slp_s) / S0
            mk_out = 1.0 if (fu_p >= 0 and fu_s >= 0) else (0.5 if (fu_p >= 0 or fu_s >= 0) else 0.0)
            x_open = False
        gross = side * ((F0 - F1) + (S1 - S0)) / S0
        # funding event at minute stamp tau is held iff entry_time < tau <= exit_time; with fills at the first
        # trade of a minute (open) or the last trade (close) this is e+1 <= tau <= xx in both cases
        et = e if e_open else e + 1
        xt = xx if x_open else xx + 1
        fnd = 0.0
        for tau in range(e + 1, xx + 1):
            if fund[tau] != 0.0:
                fnd += side * fund[tau] * Sc[tau - 1] * (1.0 + bc[tau - 1]) / S0
        hours = max(1.0, np.ceil((xt - et) / 60.0))
        ru = 0.0
        for tau in range(et, xt + 1):
            ru += rusdt[tau]
        ru = ru / max(1, xt - et + 1)
        net_unlev = gross - c_in - fee_out - slp_out + fnd
        # ---------------- margin path, per leverage ----------------
        last = xx - 1 if x_open else xx
        rec = out[nt]
        rec[0] = t; rec[1] = e; rec[2] = xx; rec[3] = side; rec[4] = gross; rec[5] = fee_p_in + fee_s_in + fee_out
        rec[6] = slp_p_in + slp_s_in + slp_out; rec[7] = fnd; rec[8] = hours; rec[9] = mk_in; rec[10] = mk_out
        rec[11] = (F0 / S0 - 1.0); rec[12] = d
        for li in range(NLv):
            L = levs[li]
            eq0 = 1.0 - L * c_in
            # ---- feasibility at the opening (initial margin) ----
            if side == 1:
                rej_mc = (eq0 - haircut * L) < (im * L + 0.10 * max(0.0, L - eq0))
            else:
                rej_mc = eq0 < (im * L + 0.10 * L)
            rej_pm = eq0 < 1.3 * m_pm * L
            A = max(0.5 * eq0, 0.10 * L + L * c_in_s)       # spot-margin account (after its fees)
            B = eq0 - A
            rej_sep = B < im * L
            # ---- interest (per unit equity) ----
            if side == 1:
                int_j = max(0.0, L - eq0) * ru * hours / 8760.0
                int_s = max(0.0, L - A) * ru * hours / 8760.0
            else:
                int_j = L * r_coin * hours / 8760.0
                int_s = int_j
            fin_mc = 1.0 + L * net_unlev - int_j
            fin_pm = fin_mc
            fin_sep = 1.0 + L * net_unlev - int_s
            worst = eq0
            liq_mc = False; liq_pm = False; liq_sep = False
            post_mc = 0.0; post_pm = 0.0; post_sep = 0.0
            for tau in range(e, last + 1):
                v = Sc[tau] / S0
                I = v * (1.0 + ic[tau])
                if side == 1:
                    if stress == 0:
                        Mw = I * (1.0 + max(mpc[tau], mpc[tau - 1]))
                    else:
                        Mw = I * (1.0 + mpc[tau] + max(0.0, ph[tau]))
                    unreal = (F0 / S0 - Mw) + (I - 1.0)
                else:
                    if stress == 0:
                        Mw = I * (1.0 + min(mpc[tau], mpc[tau - 1]))
                    else:
                        Mw = I * (1.0 + mpc[tau] + min(0.0, pl[tau]))
                    unreal = (Mw - F0 / S0) + (1.0 - I)
                eq = eq0 + L * unreal
                if eq < worst:
                    worst = eq
                if not liq_mc and not rej_mc:
                    if side == 1:
                        sv = L * I
                        adj = eq - haircut * sv
                        mmreq = mm * L * Mw + bmmr * max(0.0, sv - eq)
                    else:
                        adj = eq
                        mmreq = mm * L * Mw + cbmmr * L * I
                    if adj < mmreq:
                        liq_mc = True
                        post_mc = max(0.0, eq - (lp + mm + fp_t + slip_base) * L * v)
                if not liq_pm and not rej_pm:
                    if eq < m_pm * L * v:
                        liq_pm = True
                        post_pm = max(0.0, eq - (lp + mm + fp_t + slip_base) * L * v)
                if not liq_sep and not rej_sep:
                    if side == 1:
                        Mh = Sc[tau] * (1.0 + mh[tau]) / S0
                        Slo = Sc[tau] * (1.0 + sl[tau]) / S0
                        lp_ = B + L * (F0 / S0 - Mh) < mm * L * Mh
                        ls_ = A + L * (Slo - 1.0) < mmr_m * L * Slo
                    else:
                        Ml = Sc[tau] * (1.0 + ml[tau]) / S0
                        Shi = Sc[tau] * (1.0 + sh[tau]) / S0
                        lp_ = B + L * (Ml - F0 / S0) < mm * L * Ml
                        ls_ = A + L * (1.0 - Shi) < mmr_m * L * Shi
                    if lp_ or ls_:
                        liq_sep = True
                        Fc_ = v * (1.0 + bc[tau])
                        if lp_ and ls_:
                            post_sep = 0.0
                        elif lp_:
                            post_sep = max(0.0, A + side * L * (v - 1.0) - L * v * (fs_t + slip_base))
                        else:
                            post_sep = max(0.0, B + side * L * (F0 / S0 - Fc_) - L * Fc_ * (fp_t + slip_base))
            base = 13
            vals_f = (post_mc if liq_mc else fin_mc, post_pm if liq_pm else fin_pm, post_sep if liq_sep else fin_sep)
            liqs = (liq_mc, liq_pm, liq_sep)
            rejs = (rej_mc, rej_pm, rej_sep)
            for mi in range(3):
                fv = vals_f[mi]
                if rejs[mi]:
                    rec[base + (mi * 4 + 0) * NLv + li] = 0.0
                    rec[base + (mi * 4 + 1) * NLv + li] = 0.0
                    rec[base + (mi * 4 + 2) * NLv + li] = 0.0
                    rec[base + (mi * 4 + 3) * NLv + li] = 1.0
                else:
                    rec[base + (mi * 4 + 0) * NLv + li] = fv - 1.0
                    rec[base + (mi * 4 + 1) * NLv + li] = min(worst, fv) - 1.0
                    rec[base + (mi * 4 + 2) * NLv + li] = 1.0 if liqs[mi] else 0.0
                    rec[base + (mi * 4 + 3) * NLv + li] = 0.0
        nt += 1
        if nt >= out.shape[0]:
            break
        t = xx + 1
    return out[:nt]
