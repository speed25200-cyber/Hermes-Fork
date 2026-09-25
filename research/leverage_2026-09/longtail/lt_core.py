"""Core of the long-tail study: data access, portfolio construction and an hour-by-hour simulator (numba).

Simulator conventions (see prereg.json):
- decision at the close of bar k (data <= k); executed at close of bar k+lat (lat = 0 base, 1 = one extra bar);
- fixed quantities between rebalances; hourly P&L on ffilled Binance closes; funding stamped on the bar ending at
  the funding time is paid by the position held during that bar (notional at that close);
- cost per side = (fee + max(floor, exp(a + b ln ADV + c ln |order USDT|))) bp x cost_mult;
- OKX lots (quantity rounded toward zero), tiered MMR, intrabar extremes (lows for longs, highs for shorts, all at
  once) for drawdown and liquidation; liquidation = worst intrabar equity <= maintenance margin -> account ends.
"""
import json, sys
import numpy as np, pandas as pd
from numba import njit

sys.path.insert(0, "/home/user/Hermes/src")
W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"
SIGS = ["r1h", "r4h", "r1d", "m3d", "m7d", "m30d", "fund", "dfund", "vshock", "age", "rr1d", "rm7d"]
IS0, IS1 = pd.Timestamp("2022-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC")
OOS0, OOS1 = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2026-09-01", tz="UTC")
UNIV = {"TAIL": (31, 150), "TOP30": (1, 30)}
COST_A, COST_B, COST_C = 6.745, -0.438, 0.317
FEE_BP = 5.0


class Data:
    def __init__(self):
        f = np.load(f"{W}/data/features.npz")
        self.f = {k: f[k] for k in f.files}
        s = np.load(f"{W}/data/simarrays.npz")
        self.Cf, self.H, self.Lo, self.F = s["Cf"], s["H"], s["Lo"], s["F"]
        self.hrs = s["hrs"]
        self.syms = [str(x) for x in s["syms"]]
        self.N = len(self.syms)
        self.K = self.f["K"]
        try:  # mark-price extremes for margin/drawdown (exchanges liquidate on mark), last-price extremes as fallback
            mk = np.load(f"{W}/data/mark_hl.npz")
            self.XH = np.where(np.isfinite(mk["MH"]), mk["MH"], self.H)
            self.XL = np.where(np.isfinite(mk["ML"]), mk["ML"], self.Lo)
        except FileNotFoundError:
            self.XH, self.XL = self.H, self.Lo
        self.dec_time = pd.to_datetime((self.f["hrs"] + 1) * 3600, unit="s", utc=True)
        self._okx()

    def _okx(self):
        from hermes.execution.okx.instruments import okx_inst_id, binance_price_factor
        inst = pd.DataFrame(json.load(open(f"{W}/venue/okx_instruments_now.json")))
        inst = inst[inst.instId.str.endswith("-USDT-SWAP") & (inst.instCategory == "1")].set_index("instId")
        tiers = json.load(open(f"{W}/venue/okx_tiers_now.json"))
        N = self.N
        self.lot_q = np.zeros(N)  # Binance units per lot, 0 = unknown -> 1 USDT lots
        self.tier_max = np.full((N, 12), np.inf)
        self.tier_mmr = np.full((N, 12), np.nan)
        self.tier_usd = np.zeros(N, np.bool_)
        known_usd_ladders = []
        last = self.Cf[-1]
        for j, s in enumerate(self.syms):
            iid = okx_inst_id(s)
            fac = binance_price_factor(s)
            if iid in inst.index and inst.loc[iid, "state"] == "live":
                ct = float(inst.loc[iid, "ctVal"]); lot = float(inst.loc[iid, "lotSz"])
                self.lot_q[j] = lot * ct / fac
                fam = inst.loc[iid, "instFamily"]
                t = tiers.get(fam)
                if t:
                    for i, row in enumerate(t[:12]):
                        self.tier_max[j, i] = float(row["maxSz"]) * ct / fac
                        self.tier_mmr[j, i] = float(row["mmr"])
                    if np.isfinite(last[j]):
                        known_usd_ladders.append([(float(r["maxSz"]) * ct / fac * last[j], float(r["mmr"])) for r in t[:12]])
        # names not on OKX today: median ladder of known names in USD (tiers compared with notional)
        L = min(len(x) for x in known_usd_ladders)
        med_max = np.median([[x[i][0] for i in range(L)] for x in known_usd_ladders], axis=0)
        med_mmr = np.median([[x[i][1] for i in range(L)] for x in known_usd_ladders], axis=0)
        for j in range(N):
            if not np.isfinite(self.tier_mmr[j, 0]):
                self.tier_usd[j] = True
                self.tier_max[j, :L] = med_max; self.tier_mmr[j, :L] = med_mmr
        self.tier_mmr = np.where(np.isfinite(self.tier_mmr), self.tier_mmr, np.nanmax(self.tier_mmr, axis=1, keepdims=True))

    def rows(self, R, t0, t1):
        hr = self.f["hrs"]
        m = ((hr + 1) % R == 0) & (self.dec_time >= t0) & (self.dec_time < t1)
        return np.where(m)[0]

    def elig(self, univ, rows, need=None):
        lo, hi = UNIV[univ]
        rk = self.f["rank"][rows]
        e = (rk >= lo) & (rk <= hi) & self.f["alive"][rows] & np.isfinite(self.f["vol"][rows])
        e &= np.isfinite(self.f["r1d"][rows])
        if need is not None:
            e &= np.isfinite(self.f[need][rows])
        return e


def pct_rank(x, e):
    """Cross-sectional percentile in (0,1) among eligible entries per row; NaN elsewhere."""
    out = np.full(x.shape, np.nan, np.float32)
    for i in range(x.shape[0]):
        idx = np.where(e[i] & np.isfinite(x[i]))[0]
        n = len(idx)
        if n == 0:
            continue
        o = np.argsort(np.argsort(x[i, idx], kind="stable"), kind="stable")
        out[i, idx] = (o + 0.5) / n
    return out


def ic_series(sig_pct, fwd, e):
    ics = np.full(sig_pct.shape[0], np.nan)
    for i in range(sig_pct.shape[0]):
        idx = np.where(e[i] & np.isfinite(sig_pct[i]) & np.isfinite(fwd[i]))[0]
        if len(idx) < 8:
            continue
        a = sig_pct[i, idx]
        b = np.argsort(np.argsort(fwd[i, idx]))
        ics[i] = np.corrcoef(a, b)[0, 1]
    return ics


def build_weights(score, e, vol, beta, q=0.25, hyst=False, neutral="dollar", min_names=8):
    """score: rows x N (NaN = ineligible). Returns rows x N target weights (gross 1)."""
    nR, N = score.shape
    Wt = np.zeros((nR, N))
    prevL = np.zeros(N, bool); prevS = np.zeros(N, bool)
    for i in range(nR):
        ok = e[i] & np.isfinite(score[i])
        idx = np.where(ok)[0]
        n = len(idx)
        if n < min_names:
            prevL[:] = False; prevS[:] = False
            continue
        s = score[i, idx]
        o = np.argsort(np.argsort(s, kind="stable"), kind="stable")
        pct = (o + 0.5) / n
        nl = max(1, int(round(q * n)))
        L = np.zeros(N, bool); S = np.zeros(N, bool)
        L[idx[o >= n - nl]] = True
        S[idx[o < nl]] = True
        if hyst:
            keepL = prevL[idx] & (pct >= 0.5)
            keepS = prevS[idx] & (pct <= 0.5)
            L[idx[keepL]] = True
            S[idx[keepS]] = True
            both = L & S
            L[both] = False; S[both] = False
        v = vol[i].astype(np.float64)
        vfloor = np.nanpercentile(v[idx], 20)
        v = np.where(np.isfinite(v), np.maximum(v, vfloor), np.nan)
        w = np.zeros(N)
        for leg, sgn in ((L, 1.0), (S, -1.0)):
            j = np.where(leg)[0]
            if len(j) == 0:
                continue
            iv = 1.0 / v[j]
            ww = iv / iv.sum()
            cap = 2.0 / len(j)
            for _ in range(5):
                over = ww > cap
                if not over.any():
                    break
                excess = (ww[over] - cap).sum()
                ww[over] = cap
                ww[~over] += excess * ww[~over] / ww[~over].sum()
            w[j] = sgn * ww
        if neutral == "beta":
            b = beta[i].astype(np.float64)
            b = np.clip(np.where(np.isfinite(b), b, 1.0), 0.2, 3.0)
            BL = (w[L] * b[L]).sum() if L.any() else 1.0
            BS = (-w[S] * b[S]).sum() if S.any() else 1.0
            a_, b_ = BS / (BL + BS), BL / (BL + BS)
            w[L] *= a_; w[S] *= b_
        else:
            w[L] *= 0.5; w[S] *= 0.5
        Wt[i] = w
        prevL, prevS = L, S
    return Wt


@njit(cache=True)
def _sim(Cf, H, Lo, F, h0, h1, ex_h, Wt, advr, floor, lev, E0, cost_mult, lot_q, tier_max, tier_mmr, tier_usd, fee_bp,
         ca, cb, cc, use_lots):
    T, N = Cf.shape
    nH = h1 - h0 + 1
    eq = np.zeros(nH); worst = np.zeros(nH)
    q = np.zeros(N)
    pnl_coin = np.zeros(N)
    E = E0
    peak = E0
    ptr = 0
    nD = ex_h.shape[0]
    liq_h = -1
    tot_cost = 0.0; tot_fund = 0.0; tot_turn = 0.0; tier_breach = 0
    for h in range(h0, h1 + 1):
        # 1) P&L of bar h on positions held during it
        if h > h0:
            w_eq = E
            mm = 0.0
            pn = 0.0
            fu = 0.0
            for i in range(N):
                if q[i] != 0.0:
                    p0 = Cf[h - 1, i]
                    x = Lo[h, i] if q[i] > 0 else H[h, i]
                    w_eq += q[i] * (x - p0)
                    size = abs(q[i]) * x if tier_usd[i] else abs(q[i])
                    m = tier_mmr[i, tier_mmr.shape[1] - 1]
                    for t in range(tier_mmr.shape[1]):
                        if size <= tier_max[i, t]:
                            m = tier_mmr[i, t]
                            break
                    mm += abs(q[i]) * x * m
                    d = q[i] * (Cf[h, i] - p0)
                    f = q[i] * Cf[h, i] * F[h, i]
                    pn += d
                    fu += f
                    pnl_coin[i] += d - f
            E = E + pn - fu
            tot_fund += fu
            worst[h - h0] = min(w_eq, E)
            if w_eq <= mm and liq_h < 0 and lev > 0:
                liq_h = h
                E = 0.0
                for i in range(N):
                    q[i] = 0.0
                eq[h - h0] = 0.0
                worst[h - h0] = 0.0
                for hh in range(h + 1, h1 + 1):
                    eq[hh - h0] = 0.0
                    worst[hh - h0] = 0.0
                break
        else:
            worst[0] = E
        # 2) rebalance at the close of bar h if an execution is scheduled
        while ptr < nD and ex_h[ptr] < h:
            ptr += 1
        if ptr < nD and ex_h[ptr] == h and E > 0:
            for i in range(N):
                p = Cf[h, i]
                if not (p > 0):
                    continue
                tgt = Wt[ptr, i] * lev * E / p
                if use_lots:
                    lq = lot_q[i] if lot_q[i] > 0 else 1.0 / p
                    tgt = np.sign(tgt) * np.floor(abs(tgt) / lq + 1e-9) * lq
                dq = tgt - q[i]
                if dq != 0.0:
                    usd = abs(dq) * p
                    a = advr[ptr, i]
                    if not (a > 0):
                        a = 1e6
                    slip = np.exp(ca + cb * np.log(a) + cc * np.log(max(usd, 1.0)))
                    fl = floor[ptr, i]
                    if slip < fl:
                        slip = fl
                    c = usd * (fee_bp + slip) * 1e-4 * cost_mult
                    E -= c
                    tot_cost += c
                    tot_turn += usd
                    pnl_coin[i] -= c
                    q[i] = tgt
            ptr += 1
        eq[h - h0] = E
        if E > peak:
            peak = E
    return eq, worst, liq_h, tot_cost, tot_fund, tot_turn, pnl_coin


def simulate(D, rows, Wt, t0, t1, lev=1.0, E0=5000.0, lat=0, cost_mult=1.0, univ="TAIL", use_lots=True, excl=None,
             extremes="mark"):
    """rows: decision rows (feature index) with target weights Wt (rows x N)."""
    K = D.K[rows]
    ex_h = K + lat
    h0 = int(K[0])  # start at the first decision close
    h1 = int(np.searchsorted(D.hrs, (t1 - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(hours=1)) - 1)
    Wt = Wt.copy()
    if excl is not None:
        Wt[:, excl] = 0.0
    advr = np.nan_to_num(D.f["adv"][rows].astype(np.float64), nan=1e6)
    fl = np.where(D.f["rank"][rows] <= 30, 1.0, 3.0)
    if univ == "TAIL":
        fl = np.maximum(fl, 3.0)
    XH, XL = (D.XH, D.XL) if extremes == "mark" else (D.H, D.Lo)
    eq, worst, liq_h, tc, tf, tt, pc = _sim(D.Cf, XH, XL, D.F, h0, h1, ex_h.astype(np.int64), Wt, advr, fl, float(lev),
                                         float(E0), float(cost_mult), D.lot_q, D.tier_max, D.tier_mmr, D.tier_usd, FEE_BP,
                                         COST_A, COST_B, COST_C, use_lots)
    hours = pd.to_datetime((D.hrs[h0:h1 + 1] + 1) * 3600, unit="s", utc=True)
    return dict(eq=pd.Series(eq, hours), worst=pd.Series(worst, hours), liq=(None if liq_h < 0 else
                pd.to_datetime((D.hrs[liq_h] + 1) * 3600, unit="s", utc=True)), cost=tc, fund=tf, turn=tt, pnl_coin=pc)


def daily_returns(eq):
    """Close-to-close UTC-day returns, labelled by the day over which they accrue."""
    d = eq[eq.index.hour == 0]
    r = d.pct_change().dropna()
    r.index = r.index - pd.Timedelta(days=1)
    return r


def sharpe(r):
    r = np.asarray(r)
    return float(r.mean() / r.std() * np.sqrt(365)) if len(r) > 2 and r.std() > 0 else np.nan


def metrics(res, lev=1.0):
    eq = res["eq"]
    r = daily_returns(eq)
    peak = eq.cummax()
    mdd = float((1 - eq / peak).max())
    mdd_ib = float((1 - res["worst"] / peak.shift(1).fillna(eq.iloc[0])).clip(lower=0).max())
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) if eq.iloc[-1] > 0 else -1.0
    by = {int(y): float((1 + g).prod() - 1) for y, g in r.groupby(r.index.year)}
    return dict(sharpe=sharpe(r), cagr=cagr, vol=float(r.std() * np.sqrt(365)), mdd=mdd, mdd_ib=mdd_ib,
                liq=str(res["liq"]) if res["liq"] is not None else "", years=by,
                cost_frac=res["cost"] / eq.mean() / yrs, fund_frac=res["fund"] / eq.mean() / yrs,
                turn_x=res["turn"] / eq.mean() / yrs, kelly=float(r.mean() / r.var()) if r.var() > 0 else np.nan)


# ---------------------------------------------------------------------------------------------------------------
# Post-hoc variant (added after the first grid, see README): passive (maker) execution.
# At the execution close (hour h) every order is a post-only limit at Cf[h]*(1 -/+ offset). It fills during bar h+1
# only if the bar trades THROUGH the limit (low < limit for buys, high > limit for sells), at the limit price, maker fee.
# Unfilled orders are cancelled (fallback=0) or crossed at the close of bar h+1 as taker with the taker cost model
# (fallback=1). Worst-case intrabar equity adds, for filled orders, the move from the limit to the bar extreme.
@njit(cache=True)
def _sim_maker(Cf, H, Lo, XH, XL, F, h0, h1, ex_h, Wt, advr, floor, lev, E0, cost_mult, lot_q, tier_max, tier_mmr, tier_usd,
               fee_bp, ca, cb, cc, use_lots, off, fallback, maker_bp):
    T, N = Cf.shape
    nH = h1 - h0 + 1
    eq = np.zeros(nH); worst = np.zeros(nH)
    q = np.zeros(N); pend = np.zeros(N); plim = np.zeros(N); padv = np.zeros(N); pfl = np.zeros(N)
    has_pend = False
    pnl_coin = np.zeros(N)
    E = E0
    ptr = 0
    nD = ex_h.shape[0]
    liq_h = -1
    tot_cost = 0.0; tot_fund = 0.0; tot_turn = 0.0; n_ord = 0.0; n_fill = 0.0
    for h in range(h0, h1 + 1):
        if h > h0:
            w_eq = E
            mm = 0.0; pn = 0.0; fu = 0.0; cst = 0.0
            for i in range(N):
                dq = pend[i] if has_pend else 0.0
                if q[i] == 0.0 and dq == 0.0:
                    continue
                p0 = Cf[h - 1, i]
                if q[i] != 0.0:
                    x = XL[h, i] if q[i] > 0 else XH[h, i]
                    w_eq += q[i] * (x - p0)
                    d = q[i] * (Cf[h, i] - p0)
                    pn += d
                    pnl_coin[i] += d
                if dq != 0.0:
                    lim = plim[i]
                    filled = (dq > 0 and Lo[h, i] < lim) or (dq < 0 and H[h, i] > lim)
                    if filled:
                        n_fill += 1.0
                        xn = XL[h, i] if dq > 0 else XH[h, i]
                        w_eq += dq * (xn - lim)
                        d = dq * (Cf[h, i] - lim)
                        c = abs(dq) * lim * maker_bp * 1e-4 * cost_mult
                        pn += d; cst += c
                        pnl_coin[i] += d - c
                        tot_turn += abs(dq) * lim
                        q[i] += dq
                    elif fallback == 1:
                        # crossed at the close of the bar, after that bar's funding stamp (paid by the old position)
                        f0 = q[i] * Cf[h, i] * F[h, i]
                        fu += f0
                        pnl_coin[i] -= f0
                        p = Cf[h, i]
                        usd = abs(dq) * p
                        slip = np.exp(ca + cb * np.log(padv[i]) + cc * np.log(max(usd, 1.0)))
                        if slip < pfl[i]:
                            slip = pfl[i]
                        c = usd * (fee_bp + slip) * 1e-4 * cost_mult
                        cst += c
                        pnl_coin[i] -= c
                        tot_turn += usd
                        q[i] += dq
                        pend[i] = 0.0
                        # margin on the post-trade position below; funding already charged
                        if q[i] != 0.0:
                            xx = XL[h, i] if q[i] > 0 else XH[h, i]
                            size = abs(q[i]) * xx if tier_usd[i] else abs(q[i])
                            m = tier_mmr[i, tier_mmr.shape[1] - 1]
                            for t in range(tier_mmr.shape[1]):
                                if size <= tier_max[i, t]:
                                    m = tier_mmr[i, t]
                                    break
                            mm += abs(q[i]) * xx * m
                        continue
                    pend[i] = 0.0
                if q[i] != 0.0:
                    xx = XL[h, i] if q[i] > 0 else XH[h, i]
                    size = abs(q[i]) * xx if tier_usd[i] else abs(q[i])
                    m = tier_mmr[i, tier_mmr.shape[1] - 1]
                    for t in range(tier_mmr.shape[1]):
                        if size <= tier_max[i, t]:
                            m = tier_mmr[i, t]
                            break
                    mm += abs(q[i]) * xx * m
                    f = q[i] * Cf[h, i] * F[h, i]
                    fu += f
                    pnl_coin[i] -= f
            has_pend = False
            E = E + pn - fu - cst
            tot_fund += fu; tot_cost += cst
            worst[h - h0] = min(w_eq, E)
            if w_eq <= mm and liq_h < 0:
                liq_h = h
                for hh in range(h, h1 + 1):
                    eq[hh - h0] = 0.0
                    worst[hh - h0] = 0.0
                E = 0.0
                break
        else:
            worst[0] = E
        while ptr < nD and ex_h[ptr] < h:
            ptr += 1
        if ptr < nD and ex_h[ptr] == h and E > 0:
            for i in range(N):
                p = Cf[h, i]
                if not (p > 0):
                    continue
                tgt = Wt[ptr, i] * lev * E / p
                if use_lots:
                    lq = lot_q[i] if lot_q[i] > 0 else 1.0 / p
                    tgt = np.sign(tgt) * np.floor(abs(tgt) / lq + 1e-9) * lq
                dq = tgt - q[i]
                if dq != 0.0:
                    pend[i] = dq
                    plim[i] = p * (1.0 - off * 1e-4) if dq > 0 else p * (1.0 + off * 1e-4)
                    a = advr[ptr, i]
                    padv[i] = a if a > 0 else 1e6
                    pfl[i] = floor[ptr, i]
                    n_ord += 1.0
                    has_pend = True
            ptr += 1
        eq[h - h0] = E
    return eq, worst, liq_h, tot_cost, tot_fund, tot_turn, pnl_coin, n_fill / max(n_ord, 1.0)


def simulate_maker(D, rows, Wt, t0, t1, off=10.0, fallback=0, lev=1.0, E0=5000.0, lat=0, cost_mult=1.0, univ="TAIL",
                   use_lots=True, maker_bp=2.0, extremes="mark", excl=None):
    K = D.K[rows]
    ex_h = K + lat
    h0 = int(K[0])
    h1 = int(np.searchsorted(D.hrs, (t1 - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(hours=1)) - 1)
    advr = np.nan_to_num(D.f["adv"][rows].astype(np.float64), nan=1e6)
    fl = np.where(D.f["rank"][rows] <= 30, 1.0, 3.0)
    if univ == "TAIL":
        fl = np.maximum(fl, 3.0)
    XH, XL = (D.XH, D.XL) if extremes == "mark" else (D.H, D.Lo)
    if excl is not None:
        Wt = Wt.copy(); Wt[:, excl] = 0.0
    eq, worst, liq_h, tc, tf, tt, pc, fr = _sim_maker(D.Cf, D.H, D.Lo, XH, XL, D.F, h0, h1, ex_h.astype(np.int64), Wt, advr, fl,
                                                   float(lev), float(E0), float(cost_mult), D.lot_q, D.tier_max, D.tier_mmr,
                                                   D.tier_usd, FEE_BP, COST_A, COST_B, COST_C, use_lots, float(off),
                                                   int(fallback), float(maker_bp))
    hours = pd.to_datetime((D.hrs[h0:h1 + 1] + 1) * 3600, unit="s", utc=True)
    return dict(eq=pd.Series(eq, hours), worst=pd.Series(worst, hours), liq=(None if liq_h < 0 else
                pd.to_datetime((D.hrs[liq_h] + 1) * 3600, unit="s", utc=True)), cost=tc, fund=tf, turn=tt, pnl_coin=pc,
                fill_rate=fr)
