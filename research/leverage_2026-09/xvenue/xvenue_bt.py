"""Cross-venue perp arbitrage backtest: Binance USDT-M perp vs OKX USDT-M perp, same coin.

Strategy (funding-differential carry, delta-neutral across venues):
  for coin c, D_c(t) = EWMA_H( fo - fb ) * 8760  = annualised funding differential OKX minus Binance, using
  only funding already settled at t.  D>0 -> long Binance / short OKX (dir=+1); D<0 -> the reverse.
  Every 8h (00/08/16 UTC, right after settlement) hold up to K coins with |D| > th_in (a held coin is kept
  while |D| > th_out and the sign is unchanged).  Equal notional per coin and leg; same coin quantity on both
  legs (delta-neutral in coin units).

Leverage definition (primary):  L = notional held on a venue / equity on that venue
  (= gross notional of both legs / total equity; per-leg notional / total equity = L/2).
  Each venue is its own cross-margin account; the equity starts split 50/50.

Risk mechanics per venue (as the exchange does it) on 1h bars:
  * Intrabar path: from the previous mark close each leg moves to its ADVERSE mark extreme of the bar
    (long -> low, short -> high), all coins of the venue at the same time (conservative: no netting of
    intrabar timing across coins).  The other venue's legs move to the same-direction extreme.
  * Liquidation when venue equity <= maintenance margin (MMR 0.5% BTC/ETH, 1% large caps, 2% other alts).
    Then every position on that venue is closed and ALL margin left on that venue is lost (maintenance
    margin + liquidation fee).  The surviving legs on the other venue are closed at the same moment of the
    path, with forced slippage.  The account continues with what is left.
  * risk_mode='intrabar' (bot watches margin continuously): when a venue's leverage crosses k_dl*L on the
    path, both legs of every coin are cut to bring that venue back to L, at the crossing price with crash
    slippage (3x normal per leg; optional extra gap_frac * bar move on the losing leg).  risk_mode='hourly': the bot only acts at bar
    closes (liquidations then happen whenever the bar extreme crosses the liquidation level).
  * Collateral transfer between venues every R hours when the imbalance exceeds tau; arrives after D
    hours (on neither venue meanwhile).  Only wallet - initial margin (exchange leverage setting 50x) -
    unrealised losses is transferable; missing amounts are realised by closing/re-opening part of the
    winning legs (fees + slippage charged).
Costs: taker 0.05% per fill on both venues, slippage 1bp (BTC,ETH) / 3bp (large caps) / 5bp (alts), 3x on
  forced trades; funding on both legs at each venue's actual settlement times; $1 per transfer.
"""
import os, sys, json, math, itertools
import numpy as np, pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
T1 = {'BTC', 'ETH'}
T2 = {'SOL', 'XRP', 'DOGE', 'BNB', 'ADA', 'LINK', 'LTC', 'BCH', 'AVAX', 'DOT', 'TRX'}

_CACHE = {}


def load_panel():
    if 'P' in _CACHE:
        return _CACHE['P']
    Z = np.load(os.path.join(BASE, 'data', os.environ.get('XV_PANEL', 'panel.npz')), allow_pickle=True)
    coins = [str(c) for c in Z['coins']]
    time = pd.to_datetime(Z['time'].astype('int64'), unit='ns', utc=True)
    P = {'coins': coins, 'time': time}
    raw_valid = np.isfinite(Z['bc']) & np.isfinite(Z['oc']) & np.isfinite(Z['bmc']) & np.isfinite(Z['omc'])
    for k in ['bc', 'oc', 'bmc', 'omc']:
        # hold the last price through gaps (a coin whose data stops is closed at the stale price because
        # 'valid' turns False); before listing prices are 0 and never traded
        P[k] = np.nan_to_num(pd.DataFrame(Z[k]).ffill().values, nan=0.0)
    for k, ref in [('bh', 'bc'), ('bl', 'bc'), ('oh', 'oc'), ('ol', 'oc'), ('bmh', 'bmc'), ('bml', 'bmc'),
                   ('omh', 'omc'), ('oml', 'omc')]:
        a = Z[k].copy()
        bad = ~np.isfinite(a)
        a[bad] = P[ref][bad]
        P[k] = a
    P['fb'] = np.nan_to_num(Z['fb'])
    P['fo'] = np.nan_to_num(Z['fo'])
    first = np.argmax(raw_valid, axis=0)
    first[~raw_valid.any(axis=0)] = len(time)
    age = np.arange(len(time))[:, None] - first[None, :]
    P['valid'] = raw_valid & (age >= 24 * 7)
    P['listed'] = age >= 0
    P['slip'] = np.array([1e-4 if c in T1 else (3e-4 if c in T2 else 5e-4) for c in coins])
    P['mmr'] = np.array([0.005 if c in T1 else (0.01 if c in T2 else 0.02) for c in coins])
    _CACHE['P'] = P
    return P


def signal(P, H):
    key = ('sig', H, id(P))
    if key in _CACHE:
        return _CACHE[key]
    g = P['fo'] - P['fb']
    g = np.where(P['listed'], g, np.nan)
    D = pd.DataFrame(g).ewm(halflife=H, ignore_na=True, min_periods=72).mean().values * 8760.0
    D = np.where(P['listed'], D, np.nan)
    _CACHE[key] = D
    return D


def spread_dev(P, win=24 * 7):
    """Cross-venue price deviation in bp: log(P_binance/P_okx) minus its trailing 7d median (causal)."""
    key = ('spr', win, id(P))
    if key in _CACHE:
        return _CACHE[key]
    with np.errstate(divide='ignore', invalid='ignore'):
        s = np.log(P['bc'] / P['oc'])
    s = np.where(P['valid'], s, np.nan)
    S = pd.DataFrame(s)
    med = S.rolling(win, min_periods=24).median().shift(1)
    dev = ((S - med) * 1e4).values
    _CACHE[key] = dev
    return dev


DEFAULT = dict(L=1.0, H=72, th_in=0.10, th_out=0.05, K=5, R=4, D=4, tau=0.05, k_dl=1.5, delta=0.25,
               fee=0.0005, fee_maker_leg=None, lset=50.0, liq_price='mark', risk_mode='intrabar', liq_mode='co',
               gap_frac=0.0, universe='all', start='2022-01-01', end='2025-01-01', eq0=100000.0,
               transfer_fee=1.0, decide_every=8, slip_mult=1.0, fixed_dir=0,
               strategy='funding', sp_in=50.0, sp_out=10.0, sp_lag=1, sp_maxhold=48, balance=False,
               base_mode='avg')


def run(params, P=None, record=False):
    p = dict(DEFAULT)
    p.update(params)
    P = P or load_panel()
    coins = P['coins']
    time = P['time']
    C = len(coins)
    i0 = max(1, int(np.searchsorted(time, pd.Timestamp(p['start'], tz='UTC'))))
    i1 = int(np.searchsorted(time, pd.Timestamp(p['end'], tz='UTC')))
    Dsig = signal(P, p['H'])
    if p['universe'] == 'btceth':
        umask = np.array([c in T1 for c in coins])
    elif p['universe'] == 'majors':
        umask = np.array([c in T1 or c in T2 for c in coins])
    elif p['universe'] in coins:
        umask = np.array([c == p['universe'] for c in coins])
    else:
        umask = np.ones(C, bool)
    L = float(p['L'])
    K = int(p['K'])
    mmr = P['mmr']
    kL = p['k_dl'] * L
    kL = min(kL, 0.8 / float(np.max(mmr[umask]))) if L > 0 else kL   # de-risk strictly before liquidation
    fee = p['fee']
    slip = P['slip'] * p['slip_mult']
    CL = [P['bc'], P['oc']]           # last price (fills at bar close)
    MK = [P['bmc'], P['omc']]         # mark close (equity, funding)
    if p['liq_price'] == 'mark':
        HI = [P['bmh'], P['omh']]
        LO = [P['bml'], P['oml']]
    else:
        HI = [P['bh'], P['oh']]
        LO = [P['bl'], P['ol']]
    MH = [P['bmh'], P['omh']]
    ML = [P['bml'], P['oml']]
    FR = [P['fb'], P['fo']]
    valid = P['valid']
    intrabar = p['risk_mode'] == 'intrabar'
    SPR = spread_dev(P) if p['strategy'] == 'spread' else None
    held_h = np.zeros(C, int)

    W = np.array([p['eq0'] / 2, p['eq0'] / 2])
    q = np.zeros((2, C))
    ent = np.zeros((2, C))
    dirn = np.zeros(C, int)
    inflight = []  # (arrival index, venue, amount)
    st = dict(liq=0, liq_events=[], fills=0, entries=0, dlev=0, cuts=0, transfers=0, realize=0, fees=0.0,
              funding=0.0, slipcost=0.0, liqloss=0.0)
    n = i1 - i0
    eq_close = np.zeros(n)
    eq_low = np.zeros(n)
    lev_rec = np.zeros(n) if record else None
    q_rec = np.zeros((n, 2, C)) if record else None
    fee_m = p['fee_maker_leg']

    def trade(v, c, dq, price, forced=False, extra=0.0, why='strategy'):
        """Trade dq coins on venue v at `price` (+slippage); update wallet / average entry."""
        if dq == 0 or price <= 0:
            return
        sl = slip[c] * (3.0 if forced else 1.0) + extra
        f = fee
        if fee_m is not None and not forced and v == 1:
            f, sl = fee_m, 0.0  # OKX leg worked as a resting maker order (sensitivity only)
        px = price * (1 + sl * np.sign(dq))
        cost = abs(dq) * price * f
        st['fees'] += cost
        st['slipcost'] += abs(dq) * price * sl
        st['cost_' + why] = st.get('cost_' + why, 0.0) + cost + abs(dq) * price * sl
        st['fills'] += 1
        q0 = q[v, c]
        q1 = q0 + dq
        if q0 == 0 or np.sign(q0) == np.sign(dq):
            ent[v, c] = (ent[v, c] * abs(q0) + px * abs(dq)) / abs(q1)
        else:
            closed = min(abs(dq), abs(q0))
            W[v] += closed * np.sign(q0) * (px - ent[v, c])
            if abs(dq) > abs(q0):
                ent[v, c] = px
            elif abs(q1) < 1e-15:
                ent[v, c] = 0.0
                q1 = 0.0
        q[v, c] = q1
        W[v] -= cost

    def eqv(v, prices):
        return W[v] + np.sum(q[v] * (prices - ent[v]))

    def liquidate(v, t, xv, xu):
        """Venue v liquidated when its legs are at prices xv; surviving legs on u closed at xu."""
        u = 1 - v
        st['liq'] += 1
        st['liq_events'].append((str(time[t]), ['binance', 'okx'][v], [coins[c] for c in np.nonzero(q[v])[0]]))
        st['liqloss'] += max(eqv(v, xv), 0.0)
        q[v] = 0.0
        ent[v] = 0.0
        W[v] = 0.0  # everything left on that venue (maintenance margin + liquidation fee) is lost
        for c in np.nonzero(q[u])[0]:
            trade(u, c, -q[u, c], xu[c], forced=True, why='liq_close')
        dirn[:] = 0

    for k, t in enumerate(range(i0, i1)):
        pc = [CL[0][t], CL[1][t]]
        pm = [MK[0][t], MK[1][t]]
        has = [bool(np.any(q[0] != 0)), bool(np.any(q[1] != 0))]
        # ---- intrabar worst total equity of the bar that just ended (positions held during it) ----
        if has[0] or has[1]:
            infl0 = sum(a for (_, _, a) in inflight)
            lo = sum(W[v] + np.sum(q[v] * (ML[v][t] - ent[v])) for v in (0, 1)) + infl0
            hi = sum(W[v] + np.sum(q[v] * (MH[v][t] - ent[v])) for v in (0, 1)) + infl0
            bar_low = min(lo, hi)
        else:
            bar_low = np.inf
        # ---- 1. intrabar path: de-risk (intrabar mode) and liquidation ----
        for v in (0, 1):
            if not np.any(q[v] != 0):
                continue
            u = 1 - v
            o_v = MK[v][t - 1]
            o_u = MK[u][t - 1]
            if p['liq_mode'] == 'sum':
                # every leg on v at its own adverse extreme at the same time (conservative)
                scen = [(np.where(q[v] > 0, LO[v][t], HI[v][t]), np.where(q[v] > 0, LO[u][t], HI[u][t]))]
            else:
                # co-moving market: all coins at their bar low, or all at their bar high (worse one first)
                sl_ = W[v] + np.sum(q[v] * (LO[v][t] - ent[v]))
                sh_ = W[v] + np.sum(q[v] * (HI[v][t] - ent[v]))
                scen = [(LO[v][t], LO[u][t]), (HI[v][t], HI[u][t])]
                if sh_ < sl_:
                    scen = scen[::-1]
            dead = False
            for adv, ext_u in scen:
                if dead or not np.any(q[v] != 0):
                    break
                lam = 0.0
                for _it in range(50):
                    x0 = o_v + lam * (adv - o_v)
                    dx = (1 - lam) * (adv - o_v)                 # remaining move to the extreme
                    E0 = W[v] + np.sum(q[v] * (x0 - ent[v]))
                    a = np.sum(q[v] * dx)                        # equity change along the remaining path
                    N0 = np.sum(np.abs(q[v]) * x0)
                    b = np.sum(np.abs(q[v]) * dx)
                    M0 = np.sum(mmr * np.abs(q[v]) * x0)
                    m = np.sum(mmr * np.abs(q[v]) * dx)
                    if E0 <= M0:
                        s_liq = 0.0
                    elif a - m < 0:
                        s_liq = (E0 - M0) / (m - a)
                    else:
                        s_liq = np.inf
                    s_cut = np.inf
                    if intrabar and L > 0:
                        if N0 >= kL * E0 and E0 > M0:
                            s_cut = 0.0
                        elif b - kL * a > 0:
                            s_cut = (kL * E0 - N0) / (b - kL * a)
                    if 0.0 <= s_cut <= 1.0 and s_cut <= s_liq:
                        lam_c = lam + s_cut * (1 - lam)
                        xv = o_v + lam_c * (adv - o_v)
                        xu = o_u + lam_c * (ext_u - o_u)
                        Ev = W[v] + np.sum(q[v] * (xv - ent[v]))
                        Nv = np.sum(np.abs(q[v]) * xv)
                        frac = 1.0 - min(1.0, L * max(Ev, 0.0) / Nv) if Nv > 0 else 0.0
                        frac = max(frac, 1.0 - 1.0 / p['k_dl'])
                        movefrac = np.abs(adv - o_v) / np.where(o_v > 0, o_v, 1.0)
                        gap = p['gap_frac'] * movefrac
                        st['cuts'] += 1
                        if p.get('debug', False):
                            print('CUT', time[t], ['B', 'O'][v], 'lev_at_cut', round(Nv / Ev, 1) if Ev > 0 else None, 'E', [round(eqv(0, MK[0][t - 1])), round(eqv(1, MK[1][t - 1]))],
                                  'W', [round(W[0]), round(W[1])], 'pend', [round(sum(a for (_, vv, a) in inflight if vv == j)) for j in (0, 1)],
                                  'move%', np.round(100 * (adv - o_v) / np.where(o_v > 0, o_v, 1), 2)[np.nonzero(q[v])[0]])
                        for c in np.nonzero(q[v])[0]:
                            # the pair moves together, so a late fill costs only on the leg filled late;
                            # gap_frac (sensitivity) charges it on the losing leg only
                            trade(v, c, -q[v, c] * frac, xv[c], forced=True, extra=gap[c], why='cut')
                            trade(u, c, -q[u, c] * frac, xu[c], forced=True, why='cut')
                        lam = lam_c + 1e-9
                        if not np.any(q[v] != 0):
                            break
                        continue
                    if s_liq <= 1.0:
                        lam_l = lam + s_liq * (1 - lam)
                        xv = o_v + lam_l * (adv - o_v)
                        xu = o_u + lam_l * (ext_u - o_u)
                        liquidate(v, t, xv, xu)
                        dead = True
                    break
        has = [bool(np.any(q[0] != 0)), bool(np.any(q[1] != 0))]
        # ---- 2. funding settled at t ----
        for v in (0, 1):
            if has[v]:
                fr = FR[v][t]
                if np.any(fr != 0):
                    pay = np.sum(q[v] * pm[v] * fr)
                    W[v] -= pay
                    st['funding'] -= pay
        # ---- 3. transfers arriving ----
        if inflight:
            keep = []
            for (ta, v, amt) in inflight:
                if ta <= t:
                    W[v] += amt
                else:
                    keep.append((ta, v, amt))
            inflight = keep
        pend = [sum(a for (_, v, a) in inflight if v == 0), sum(a for (_, v, a) in inflight if v == 1)]
        # ---- 4. de-risk check at the bar close (both modes) ----
        if has[0] or has[1]:
            levs = []
            for v in (0, 1):
                N = np.sum(np.abs(q[v]) * pm[v])
                Em = eqv(v, pm[v])
                levs.append(N / Em if Em > 0 else np.inf)
            worst = max(levs)
            if worst > kL:
                s = L / worst if np.isfinite(worst) else 0.0
                st['dlev'] += 1
                for c in np.nonzero(dirn)[0]:
                    for v in (0, 1):
                        trade(v, c, -q[v, c] * (1 - s), pc[v][c], forced=True, why='cut')
        E = [eqv(0, pc[0]), eqv(1, pc[1])]
        # ---- 5. collateral rebalancing between venues ----
        if (t % p['R']) == 0:
            tot = E[0] + E[1] + pend[0] + pend[1]
            gap_ = (E[0] + pend[0]) - (E[1] + pend[1])
            if tot > 0 and abs(gap_) / tot > p['tau']:
                r = 0 if gap_ > 0 else 1
                d = 1 - r
                X = abs(gap_) / 2
                up = q[r] * (pc[r] - ent[r])
                Nr = np.sum(np.abs(q[r]) * pc[r])
                avail = W[r] - Nr / p['lset'] + min(0.0, up.sum())
                if X > avail and np.any(up > 0):
                    pos = up > 0
                    need = X - max(avail, 0.0)
                    fr_ = min(1.0, need / up[pos].sum())
                    for c in np.nonzero(pos)[0]:
                        dq = q[r, c] * fr_
                        trade(r, c, -dq, pc[r][c], why='realize')
                        trade(r, c, dq, pc[r][c], why='realize')
                    st['realize'] += 1
                    up = q[r] * (pc[r] - ent[r])
                    Nr = np.sum(np.abs(q[r]) * pc[r])
                    avail = W[r] - Nr / p['lset'] + min(0.0, up.sum())
                X = min(X, max(avail, 0.0))
                if X > 10 * p['transfer_fee']:
                    W[r] -= X
                    inflight.append((t + p['D'], d, X - p['transfer_fee']))
                    st['transfers'] += 1
                    pend[d] += X - p['transfer_fee']
                E = [eqv(0, pc[0]), eqv(1, pc[1])]
        # ---- 6. strategy decision (every 8h, right after settlement) ----
        spread_mode = p['strategy'] == 'spread'
        if spread_mode or (time[t].hour % p['decide_every']) == 0:
            Dt = Dsig[t]
            ok = valid[t] & umask & np.isfinite(Dt)
            if p['base_mode'] == 'min':
                base = min(E[0] + pend[0], E[1] + pend[1])
            else:  # half of total equity (incl. transfers in flight); imbalance handled by transfers/cuts
                base = 0.5 * (E[0] + E[1] + pend[0] + pend[1])
            new = np.zeros(C, int)
            if spread_mode:
                # price-divergence reversion: signal observed sp_lag hours ago must still be on (no same-bar fill)
                dv = SPR[t - p['sp_lag']] if p['sp_lag'] > 0 else SPR[t]
                dnow = SPR[t]
                ok2 = valid[t] & umask & np.isfinite(dv) & np.isfinite(dnow)
                for c in np.nonzero(dirn)[0]:
                    held_h[c] += 1
                    if ok2[c] and held_h[c] < p['sp_maxhold'] and -np.sign(dnow[c]) == dirn[c] and abs(dnow[c]) > p['sp_out']:
                        new[c] = dirn[c]
                slots = K - int(np.sum(new != 0))
                order = np.argsort(-np.abs(np.nan_to_num(dv)))
                for c in order:
                    if slots <= 0:
                        break
                    if ok2[c] and new[c] == 0 and dirn[c] == 0 and abs(dv[c]) > p['sp_in'] and np.sign(dv[c]) == np.sign(dnow[c]) \
                            and abs(dnow[c]) > p['sp_in'] / 2:
                        new[c] = -int(np.sign(dnow[c]))
                        held_h[c] = 0
                        slots -= 1
            elif p['fixed_dir'] != 0:
                for c in np.nonzero(ok)[0][:K]:
                    new[c] = p['fixed_dir']
            else:
                held = [c for c in np.nonzero(dirn)[0]
                        if ok[c] and np.sign(Dt[c]) == dirn[c] and abs(Dt[c]) > p['th_out']]
                held = sorted(held, key=lambda c: -abs(Dt[c]))[:K]
                order = [c for c in np.argsort(-np.abs(np.nan_to_num(Dt)))
                         if ok[c] and abs(Dt[c]) > p['th_in'] and c not in held]
                if p['balance']:
                    # venue-neutral book: as many coins long-on-Binance as short-on-Binance, so each venue's
                    # net market exposure is ~0 and price moves shift little equity between venues
                    side = {1: [c for c in held if dirn[c] == 1], -1: [c for c in held if dirn[c] == -1]}
                    for sg in (1, -1):
                        side[sg] = side[sg] + [c for c in order if np.sign(Dt[c]) == sg]
                    m_ = min(len(side[1]), len(side[-1]), K // 2)
                    for sg in (1, -1):
                        for c in side[sg][:m_]:
                            new[c] = sg
                else:
                    for c in held:
                        new[c] = dirn[c]
                    slots = K - len(held)
                    for c in order:
                        if slots <= 0:
                            break
                        new[c] = int(np.sign(Dt[c]))
                        slots -= 1
            if base <= 0 or min(E[0], E[1]) <= 0:
                new[:] = 0
            # never size so that a venue whose collateral is still in transit starts above 0.8*k_dl*L
            ntar = min(L * max(base, 0.0), 0.8 * kL * max(min(E[0], E[1]), 0.0)) / K
            for c in np.nonzero((new != 0) | (dirn != 0))[0]:
                mid = 0.5 * (pc[0][c] + pc[1][c])
                if not (mid > 0):
                    continue
                qt = new[c] * ntar / mid
                if new[c] == dirn[c] and new[c] != 0:
                    if abs(abs(q[0, c]) * mid - ntar) <= p['delta'] * ntar:
                        continue
                why = 'resize' if (new[c] == dirn[c] and new[c] != 0) else 'entry_exit'
                if new[c] != 0 and dirn[c] != new[c]:
                    st['entries'] += 1
                trade(0, c, qt - q[0, c], pc[0][c], why=why)
                trade(1, c, -qt - q[1, c], pc[1][c], why=why)
                dirn[c] = new[c]
            E = [eqv(0, pc[0]), eqv(1, pc[1])]
        # ---- 7. record ----
        tot_close = E[0] + E[1] + sum(a for (_, _, a) in inflight)
        eq_close[k] = tot_close
        eq_low[k] = min(tot_close, bar_low)
        if record:
            N0_ = np.sum(np.abs(q[0]) * pc[0])
            lev_rec[k] = N0_ / E[0] if E[0] > 0 else 0.0
            q_rec[k] = q
        if tot_close <= 1.0:
            eq_close[k:] = max(tot_close, 0.0)
            eq_low[k:] = max(min(eq_low[k], tot_close), 0.0)
            break
    idx = time[i0:i1]
    res = summarize(idx, eq_close, eq_low, p['eq0'])
    for kk in ['liq', 'fills', 'entries', 'dlev', 'cuts', 'transfers', 'realize']:
        res[kk] = st[kk]
    res['fees_pct'] = st['fees'] / p['eq0']
    res['slip_pct'] = st['slipcost'] / p['eq0']
    res['funding_pct'] = st['funding'] / p['eq0']
    res['liqloss_pct'] = st['liqloss'] / p['eq0']
    res['liq_events'] = st['liq_events'][:20]
    for kk in list(st.keys()):
        if kk.startswith('cost_'):
            res[kk] = st[kk] / p['eq0']
    if record:
        res['_eq'] = pd.Series(eq_close, index=idx)
        res['_eqlow'] = pd.Series(eq_low, index=idx)
        res['_lev'] = pd.Series(lev_rec, index=idx)
        res['_q'] = q_rec
    return res


def summarize(idx, eq, eqlow, eq0):
    s = pd.Series(eq, index=idx)
    yrs = (idx[-1] - idx[0]).total_seconds() / (365.25 * 86400) + 1 / 8760
    final = max(eq[-1], 0.0)
    cagr = (final / eq0) ** (1 / yrs) - 1 if final > 0 else -1.0
    peak = np.maximum.accumulate(np.concatenate([[eq0], eq]))[1:]
    dd = float(np.min(np.minimum(eqlow, eq) / peak - 1))
    daily = s.resample('D').last()
    daily = pd.concat([pd.Series([eq0], index=[idx[0] - pd.Timedelta(hours=1)]), daily])
    dr = daily.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = float(dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0.0
    yearly = {}
    for y in sorted(set(idx.year)):
        sy = s[s.index.year == y]
        prev = s[s.index.year < y]
        start = prev.iloc[-1] if len(prev) else eq0
        yearly[str(y)] = float(sy.iloc[-1] / start - 1) if start > 0 else -1.0
    return dict(cagr=float(cagr), maxdd=dd, worst_day=float(dr.min()) if len(dr) else 0.0, sharpe=sharpe,
                final=float(final / eq0), yearly=yearly)


if __name__ == '__main__':
    r = run(dict(L=float(sys.argv[1]) if len(sys.argv) > 1 else 1.0))
    print({k: v for k, v in r.items() if not k.startswith('_')})
