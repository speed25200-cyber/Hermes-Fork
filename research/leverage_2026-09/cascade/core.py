"""Core of the cascade-event study: loading, trade generators (numba), costs, slot-limited portfolio, metrics.

Conventions (see prereg.json):
- signal at the close of minute t (all features use data <= t); fills at the close of minute t+lat (lat=1 base = 60 s).
- A (fade): exchange-side stop at entry -/+ s*|R_w| (log), active from the minute after entry; fill min(stop, open).
- B/C (dislocation): exit condition checked at each minute close m >= entry; fill at close of m+lat; time exit at
  entry+Hmax.  Median frozen at the trigger minute.
- trade returns are simple returns per unit of per-leg notional; costs in bp per leg per side.
"""
import glob, json, os
import numpy as np, pandas as pd
import pyarrow.parquet as pq
from numba import njit

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade"
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
M0 = int(pd.Timestamp("2021-11-30 16:00", tz="UTC").value // 60_000_000_000)
M1 = int(pd.Timestamp("2026-09-01 16:00", tz="UTC").value // 60_000_000_000)
NM = M1 - M0
NA32 = -2**31
NA16 = -32768
IS0, IS1 = pd.Timestamp("2022-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC")
OOS0, OOS1 = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2026-09-01", tz="UTC")
DAY0 = int(IS0.value // 86400_000_000_000)
DAY_OOS0 = int(OOS0.value // 86400_000_000_000)
DAY_END = int(OOS1.value // 86400_000_000_000)
UNIV = {"TOP20": (1, 20), "TAIL": (21, 60), "ALL": (1, 60)}
COST_A, COST_B, COST_C = 6.745, -0.438, 0.317
FEE = {"perp": 5.0, "spot": 10.0, "bin": 5.0}
FEE_MAKER = {"perp": 2.0, "spot": 8.0, "bin": 2.0}
KAPPA = 0.05
SPOT_SLIP_MULT = 1.5
BORROW_BP = 1.0          # flat per trade for a short spot leg (hours-long borrow of the alt)
K_SLOTS = 5
ACCOUNT = 5000.0
FUND_OKX_END = int(pd.Timestamp("2025-09-07", tz="UTC").value // 10**6)


class Data:
    def __init__(self, syms=None):
        files = sorted(glob.glob(f"{W}/data/coins/*.trig.parquet"))
        allsyms = [os.path.basename(f).replace(".trig.parquet", "") for f in files]
        self.syms = syms or allsyms
        T, Wd, offs = [], [], [0]
        for j, s in enumerate(self.syms):
            t = pq.read_table(f"{W}/data/coins/{s}.trig.parquet").to_pandas()
            w = pq.read_table(f"{W}/data/coins/{s}.win.parquet").to_pandas()
            widx = np.searchsorted(w.t.values, t.t.values)
            assert (w.t.values[widx] == t.t.values).all()
            t["widx"] = widx + offs[-1]
            t["coin"] = j
            T.append(t); Wd.append(w); offs.append(offs[-1] + len(w))
        self.T = pd.concat(T, ignore_index=True)
        w = pd.concat(Wd, ignore_index=True)
        self.Wt = w.t.values.astype(np.int32)
        self.Wpc = w.pc.values.astype(np.int32); self.Wpo = w.po.values; self.Wph = w.ph.values; self.Wpl = w.pl.values
        self.Wsc = w.sc.values.astype(np.int32); self.Wsh = w.sh.values; self.Wsl = w.sl.values
        self.Wbc = w.bc.values.astype(np.int32); self.Wbh = w.bh.values; self.Wbl = w.bl.values
        self.Wsok = w.s_ok.values; self.Wbok = w.b_ok.values
        self.woff = np.array(offs)
        # sentinel padding so that forward walks never run off the end
        self._gate()
        self._adv()
        self.T["day"] = (self.T.t.values.astype(np.int64) * 60 // 86400).astype(np.int32)
        self.T = self.T.sort_values(["coin", "t"]).reset_index(drop=True)
        self._funding()
        self._meta()

    def _gate(self):
        cnt = np.zeros(NM, np.int32); dn = np.zeros(NM, np.int32); up = np.zeros(NM, np.int32)
        for s in self.syms:
            g = np.load(f"{W}/data/coins/{s}.gate.npz")
            cnt += np.unpackbits(g["gvalid"])[:NM].astype(np.int32)
            np.add.at(dn, g["gdn"], 1); np.add.at(up, g["gup"], 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            bdn = np.where(cnt >= 10, dn / np.maximum(cnt, 1), 0.0)
            bup = np.where(cnt >= 10, up / np.maximum(cnt, 1), 0.0)
        self.bdn, self.bup, self.gcnt = bdn, bup, cnt
        i = self.T.t.values - M0
        for lvl, sfx in ((0.25, ""), (0.50, "2")):
            gdn = pd.Series(bdn >= lvl).rolling(16, min_periods=1).max().values > 0
            gup = pd.Series(bup >= lvl).rolling(16, min_periods=1).max().values > 0
            self.T["gdn" + sfx] = gdn[i]; self.T["gup" + sfx] = gup[i]
            setattr(self, "gdn_all" + sfx, gdn); setattr(self, "gup_all" + sfx, gup)
        # amendment 2: absolute cascade gate: >= 50% of universe coins with R15 <= -5% (>= +5%) in [t-15, t]
        c15 = np.zeros(NM, np.int32); d15 = np.zeros(NM, np.int32); u15 = np.zeros(NM, np.int32)
        for s in self.syms:
            g = np.load(f"{W}/data/coins/{s}.gate3.npz")
            c15 += np.unpackbits(g["rvalid15"])[:NM].astype(np.int32)
            np.add.at(d15, g["rdn15_5"], 1); np.add.at(u15, g["rup15_5"], 1)
        cd = np.where(c15 >= 10, d15 / np.maximum(c15, 1), 0.0) >= 0.5
        cu = np.where(c15 >= 10, u15 / np.maximum(c15, 1), 0.0) >= 0.5
        cd = pd.Series(cd).rolling(16, min_periods=1).max().values > 0
        cu = pd.Series(cu).rolling(16, min_periods=1).max().values > 0
        self.T["gdnC"] = cd[i]; self.T["gupC"] = cu[i]
        self.gdn_allC, self.gup_allC = cd, cu

    def _adv(self):
        d = pd.read_parquet("/home/user/data/daily_volume_24cdb49d3f.parquet")
        adv = d.rolling(30, min_periods=7).mean().shift(1)
        day = (self.T.t.values.astype(np.int64) * 60 // 86400)
        out = np.full(len(self.T), np.nan)
        dayidx = (adv.index.values.astype("datetime64[D]").astype(np.int64))
        for j, s in enumerate(self.syms):
            m = self.T.coin.values == j
            if s in adv.columns:
                pos = np.searchsorted(dayidx, day[m])
                pos = np.clip(pos, 0, len(dayidx) - 1)
                out[m] = adv[s].values[pos]
        self.T["adv"] = np.where(np.isfinite(out) & (out > 0), out, 1e7)

    def _funding(self):
        okx = pd.read_parquet(f"{SP}/xvenue/data/okx_funding_all.parquet")
        okx = okx[okx.funding_time < FUND_OKX_END]
        bn = pd.read_parquet(f"{SP}/newlisting/data/funding_all.parquet")
        U = pd.read_csv(f"{W}/data/universe.csv").drop_duplicates("sym").set_index("sym")
        ft, fr, fb, off = [], [], [], [0]
        for s in self.syms:
            base = U.loc[s, "base"]
            o = okx[okx.instId == f"{base}-USDT-SWAP"]
            b = bn[bn.sym == s]
            # OKX where available (< 2025-09-07), Binance otherwise; Binance rate also kept for the Binance leg
            tb = (b.t.values // 60000).astype(np.int64); rb = b.rate.values
            to = (o.funding_time.values // 60000).astype(np.int64); ro = o.real_funding_rate.values
            cut = to.max() if len(to) else -1
            tt = np.concatenate([to, tb[tb > cut]]); rr = np.concatenate([ro, rb[tb > cut]])
            k = np.argsort(tt); tt, rr = tt[k], rr[k]
            # binance rate at the same stamps (nearest within 1 min) for the Binance leg
            rbin = np.zeros(len(tt))
            if len(tb):
                p = np.clip(np.searchsorted(tb, tt), 0, len(tb) - 1)
                okm = np.abs(tb[p] - tt) <= 1
                rbin[okm] = rb[p[okm]]
            ft.append(tt); fr.append(rr); fb.append(rbin); off.append(off[-1] + len(tt))
        self.fund_t = np.concatenate(ft).astype(np.int64); self.fund_r = np.concatenate(fr)
        self.fund_rb = np.concatenate(fb); self.fund_off = np.array(off, np.int64)

    def _meta(self):
        m = pd.read_csv(f"{SP}/longtail/out/okx_meta_now.csv").set_index("instFamily")
        U = pd.read_csv(f"{W}/data/universe.csv").drop_duplicates("sym").set_index("sym")
        rows = []
        for s in self.syms:
            fam = f"{U.loc[s, 'base']}-USDT"
            if fam in m.index:
                r = m.loc[fam]
                rows.append(dict(sym=s, known=True, lever=float(r.lever), mmr1=float(r.mmr1), ctVal=float(r.ctVal),
                                 lotSz=float(r.lotSz), minSz=float(r.minSz)))
            else:
                rows.append(dict(sym=s, known=False, lever=20.0, mmr1=0.02, ctVal=np.nan, lotSz=np.nan, minSz=np.nan))
        self.meta = pd.DataFrame(rows).set_index("sym")


# ----------------------------------------------------------------------------------------------------------------
@njit(cache=True)
def _valid(Wt, i, tt):
    return i < Wt.shape[0] and Wt[i] == tt


@njit(cache=True)
def gen_A(coin, t, widx, R, z, vr, rank, gdn, gup, Wt, Wpc, Wpo, Wph, Wpl,
          rlo, rhi, k, a, vmin, gate, both, H, s, lat):
    n = coin.shape[0]
    oc = np.empty(n, np.int32); ot = np.empty(n, np.int32); oe = np.empty(n, np.int32); ox = np.empty(n, np.int32)
    od = np.empty(n, np.int8); ope = np.empty(n); opx = np.empty(n); ore = np.empty(n); orx = np.empty(n)
    oty = np.empty(n, np.int8); opr = np.empty(n); orow = np.empty(n, np.int64)
    cnt = 0; cur = -1; busy = -1
    for i in range(n):
        c = coin[i]
        if c != cur:
            cur = c; busy = -1
        if t[i] <= busy:
            continue
        if rank[i] < rlo or rank[i] > rhi:
            continue
        zz = z[i]; RR = R[i]
        if not (np.isfinite(zz) and np.isfinite(RR)):
            continue
        vv = vr[i]
        if not np.isfinite(vv):
            vv = 0.0
        d = 0
        if zz <= -k and RR <= -a and vv >= vmin and (gate == 0 or gdn[i]):
            d = 1
        elif both and zz >= k and RR >= a and vv >= vmin and (gate == 0 or gup[i]):
            d = -1
        if d == 0:
            continue
        e = widx[i] + lat
        if not _valid(Wt, e, t[i] + lat) or Wpc[e] == NA32:
            continue
        lpe = Wpc[e] * 1e-5
        stop = lpe - d * s * abs(RR) if s > 0 else np.nan
        typ = 0; xm = -1; lpx = np.nan
        for h in range(1, H + 1):
            m = e + h
            if not _valid(Wt, m, t[i] + lat + h):
                break
            if Wpc[m] == NA32:
                continue
            lc = Wpc[m] * 1e-5
            if s > 0 and Wpl[m] != NA16:
                if d == 1:
                    lo = lc + Wpl[m] * 1e-4
                    if lo <= stop:
                        op = lc + Wpo[m] * 1e-4 if Wpo[m] != NA16 else stop
                        lpx = min(stop, op); xm = m; typ = 1
                        break
                else:
                    hi = lc + Wph[m] * 1e-4
                    if hi >= stop:
                        op = lc + Wpo[m] * 1e-4 if Wpo[m] != NA16 else stop
                        lpx = max(stop, op); xm = m; typ = 1
                        break
            xm = m; lpx = lc
        if xm < 0:
            continue
        oc[cnt] = c; ot[cnt] = t[i]; oe[cnt] = Wt[e]; ox[cnt] = Wt[xm]; od[cnt] = d
        ope[cnt] = lpe; opx[cnt] = lpx
        ore[cnt] = (Wph[e] - Wpl[e]) if Wph[e] != NA16 and Wpl[e] != NA16 else 0.0
        orx[cnt] = (Wph[xm] - Wpl[xm]) if Wph[xm] != NA16 and Wpl[xm] != NA16 else 0.0
        oty[cnt] = typ; opr[cnt] = abs(zz); orow[cnt] = i
        busy = Wt[xm]
        cnt += 1
    return (oc[:cnt], ot[:cnt], oe[:cnt], ox[:cnt], od[:cnt], ope[:cnt], opx[:cnt], ore[:cnt], orx[:cnt],
            oty[:cnt], opr[:cnt], orow[:cnt])


@njit(cache=True)
def gen_BC(coin, t, widx, dev, med, cg, gdn, gup, rank, Wt, Wpc, Wph, Wpl, Whc, Whh, Whl, Wok,
           rlo, rhi, k, gate, both, half, Hmax, lat):
    """dev = trigger deviation (db or dx); hedge leg close Whc = ln(hedge/perp)*1e5, so ln(perp/hedge) = -Whc*1e-5.
    d=+1: long perp (short hedge) when dev <= -k; d=-1 when dev >= k (if both)."""
    n = coin.shape[0]
    oc = np.empty(n, np.int32); ot = np.empty(n, np.int32); oe = np.empty(n, np.int32); ox = np.empty(n, np.int32)
    od = np.empty(n, np.int8); ope = np.empty(n); opx = np.empty(n); ohe = np.empty(n); ohx = np.empty(n)
    ore = np.empty(n); orx = np.empty(n); ohre = np.empty(n); ohrx = np.empty(n)
    oty = np.empty(n, np.int8); opr = np.empty(n); orow = np.empty(n, np.int64)
    cnt = 0; cur = -1; busy = -1
    for i in range(n):
        c = coin[i]
        if c != cur:
            cur = c; busy = -1
        if t[i] <= busy:
            continue
        if rank[i] < rlo or rank[i] > rhi:
            continue
        dv = dev[i]
        if not np.isfinite(dv) or not np.isfinite(med[i]):
            continue
        d = 0
        if dv <= -k:
            d = 1
        elif both and dv >= k:
            d = -1
        if d == 0:
            continue
        if gate == 1 and not cg[i]:
            continue
        if gate == 2 and not (gdn[i] or gup[i]):
            continue
        e = widx[i] + lat
        if not _valid(Wt, e, t[i] + lat) or Wpc[e] == NA32 or Whc[e] == NA32 or not Wok[e]:
            continue
        lpe = Wpc[e] * 1e-5
        lhe = lpe + Whc[e] * 1e-5
        xm = -1; typ = 0
        for h in range(0, Hmax + 1):
            m = e + h
            if not _valid(Wt, m, t[i] + lat + h):
                break
            if h == Hmax:
                xm = m; typ = 0
                break
            if Wpc[m] == NA32 or Whc[m] == NA32 or not Wok[m]:
                continue
            devm = -Whc[m] * 1e-5 - med[i]
            thr = -0.5 * k if half else 0.0
            if d * devm >= thr:
                xm = m + lat; typ = 1
                break
        if xm < 0 or not _valid(Wt, xm, Wt[e] + (xm - e)):
            continue
        # walk back to the last minute with both legs valid
        while xm > e and (Wpc[xm] == NA32 or Whc[xm] == NA32 or not Wok[xm]):
            xm -= 1
        if xm <= e:
            continue
        lpx = Wpc[xm] * 1e-5
        lhx = lpx + Whc[xm] * 1e-5
        oc[cnt] = c; ot[cnt] = t[i]; oe[cnt] = Wt[e]; ox[cnt] = Wt[xm]; od[cnt] = d
        ope[cnt] = lpe; opx[cnt] = lpx; ohe[cnt] = lhe; ohx[cnt] = lhx
        ore[cnt] = (Wph[e] - Wpl[e]) if Wph[e] != NA16 and Wpl[e] != NA16 else 0.0
        orx[cnt] = (Wph[xm] - Wpl[xm]) if Wph[xm] != NA16 and Wpl[xm] != NA16 else 0.0
        ohre[cnt] = (Whh[e] - Whl[e]) if Whh[e] != NA16 and Whl[e] != NA16 else 0.0
        ohrx[cnt] = (Whh[xm] - Whl[xm]) if Whh[xm] != NA16 and Whl[xm] != NA16 else 0.0
        oty[cnt] = typ; opr[cnt] = abs(dv); orow[cnt] = i
        busy = Wt[xm]
        cnt += 1
    return (oc[:cnt], ot[:cnt], oe[:cnt], ox[:cnt], od[:cnt], ope[:cnt], opx[:cnt], ohe[:cnt], ohx[:cnt],
            ore[:cnt], orx[:cnt], ohre[:cnt], ohrx[:cnt], oty[:cnt], opr[:cnt], orow[:cnt])


@njit(cache=True)
def fund_sum(coin, te, tx, fund_t, fund_r, fund_rb, fund_off):
    """sum of funding rates stamped in (end of entry minute, end of exit minute]; minutes -> funding minute stamps."""
    n = coin.shape[0]
    fo = np.zeros(n); fb = np.zeros(n)
    for i in range(n):
        a = fund_off[coin[i]]; b = fund_off[coin[i] + 1]
        lo = te[i] + 1; hi = tx[i] + 1
        for j in range(a, b):
            ft = fund_t[j]
            if ft >= lo and ft < hi + 0:
                fo[i] += fund_r[j]; fb[i] += fund_rb[j]
            if ft >= hi:
                break
    return fo, fb


@njit(cache=True)
def slots(te, tx, prio, K):
    """te sorted ascending (ties: prio desc). Accept if fewer than K positions open at te."""
    n = te.shape[0]
    acc = np.zeros(n, np.bool_)
    open_x = np.full(K, -1, np.int64)
    for i in range(n):
        for s in range(K):
            if open_x[s] >= 0 and open_x[s] <= te[i]:
                open_x[s] = -1
        for s in range(K):
            if open_x[s] < 0:
                open_x[s] = tx[i]; acc[i] = True
                break
    return acc


def slip_bp(rank_floor, adv, order_usd, rng_bp, mult=1.0):
    model = np.exp(COST_A + COST_B * np.log(adv) + COST_C * np.log(order_usd))
    return mult * (np.maximum(rank_floor, model) + KAPPA * rng_bp)


def floors(D, coin, rank):
    f = np.where(rank <= 20, 3.0, 6.0)
    big = np.array([s in ("BTCUSDT", "ETHUSDT") for s in D.syms])
    return np.where(big[coin], 1.0, f)


def trades_A(D, cfg, lat=1, cost_mult=1.0, order_usd=None):
    T = D.T
    w = cfg["w"]
    rlo, rhi = UNIV[cfg["univ"]]
    sfx = {"mkt2": "2", "casc": "C"}.get(cfg["gate"], "")
    out = gen_A(T.coin.values, T.t.values, T.widx.values, T[f"R{w}"].values, T[f"z{w}"].values, T[f"vr{w}"].values,
                T["rank"].values, T["gdn" + sfx].values, T["gup" + sfx].values, D.Wt, D.Wpc, D.Wpo, D.Wph, D.Wpl,
                rlo, rhi, cfg["k"], cfg["a"], cfg["vr"], 0 if cfg["gate"] == "none" else 1, cfg["side"] == "both",
                cfg["H"], cfg["s"] if cfg["s"] != "none" else 0.0, lat)
    c, t, te, tx, d, pe, px, re, rx, typ, pr, row = out
    tr = pd.DataFrame(dict(coin=c, t=t, te=te, tx=tx, d=d, typ=typ, prio=pr))
    rank = T["rank"].values[row]; adv = T.adv.values[row]
    order = order_usd if order_usd is not None else ACCOUNT / K_SLOTS
    fl = floors(D, c, rank)
    cost = 2 * FEE["perp"] + slip_bp(fl, adv, order, re) + slip_bp(fl, adv, order, rx)
    fo, fb = fund_sum(c, te, tx, D.fund_t, D.fund_r, D.fund_rb, D.fund_off)
    gross = d * (np.exp(px - pe) - 1)
    tr["gross"] = gross
    tr["cost"] = cost * cost_mult / 1e4
    tr["fund"] = -d * fo
    tr["net"] = tr.gross - tr.cost + tr.fund
    tr["rank"] = rank
    tr["pe"] = pe; tr["px"] = px
    tr["ei"] = T.widx.values[row] + lat
    tr["cost_e"] = (FEE["perp"] + slip_bp(fl, adv, order, re)) * cost_mult / 1e4
    tr["nlegs"] = 1
    return tr


def trades_BC(D, cfg, fam, lat=1, cost_mult=1.0, order_usd=None):
    T = D.T
    rlo, rhi = UNIV[cfg["univ"]]
    if fam == "B":
        dev, med, Whc, Whh, Whl, Wok = T.db.values, T.bmed.values, D.Wsc, D.Wsh, D.Wsl, D.Wsok
    else:
        dev, med, Whc, Whh, Whl, Wok = T.dx.values, T.xmed.values, D.Wbc, D.Wbh, D.Wbl, D.Wbok
    gate = {"none": 0, "coin": 1, "mkt": 2, "mkt2": 2, "casc": 2}[cfg["gate"]]
    sfx = {"mkt2": "2", "casc": "C"}.get(cfg["gate"], "")
    out = gen_BC(T.coin.values, T.t.values, T.widx.values, dev, med, T.cgate.values, T["gdn" + sfx].values,
                 T["gup" + sfx].values,
                 T["rank"].values, D.Wt, D.Wpc, D.Wph, D.Wpl, Whc, Whh, Whl, Wok,
                 rlo, rhi, cfg["k"] / 1e4, gate, cfg["side"] == "both", cfg["exit"] == "half", cfg["H"], lat)
    c, t, te, tx, d, pe, px, he, hx, re, rx, hre, hrx, typ, pr, row = out
    tr = pd.DataFrame(dict(coin=c, t=t, te=te, tx=tx, d=d, typ=typ, prio=pr))
    rank = T["rank"].values[row]; adv = T.adv.values[row]
    order = order_usd if order_usd is not None else ACCOUNT / K_SLOTS
    fl = floors(D, c, rank)
    fo, fb = fund_sum(c, te, tx, D.fund_t, D.fund_r, D.fund_rb, D.fund_off)
    perp = d * (np.exp(px - pe) - 1)
    cost = 2 * FEE["perp"] + slip_bp(fl, adv, order, re) + slip_bp(fl, adv, order, rx)
    fund = -d * fo
    two = cfg["mode"] in ("hedged", "two_leg")
    if two:
        hedge = -d * (np.exp(hx - he) - 1)
        if fam == "B":
            cost = cost + 2 * FEE["spot"] + slip_bp(fl, adv, order, hre, SPOT_SLIP_MULT) + \
                slip_bp(fl, adv, order, hrx, SPOT_SLIP_MULT) + np.where(d == 1, BORROW_BP, 0.0)
        else:
            cost = cost + 2 * FEE["bin"] + slip_bp(fl, adv, order, hre) + slip_bp(fl, adv, order, hrx)
            fund = fund + d * fb
    else:
        hedge = np.zeros_like(perp)
    tr["gross"] = perp + hedge
    tr["perp_leg"] = perp; tr["hedge_leg"] = hedge
    tr["cost"] = cost * cost_mult / 1e4
    tr["fund"] = fund
    tr["net"] = tr.gross - tr.cost + tr.fund
    tr["rank"] = rank
    tr["pe"] = pe; tr["px"] = px; tr["he"] = he; tr["hx"] = hx
    tr["ei"] = T.widx.values[row] + lat
    ce = FEE["perp"] + slip_bp(fl, adv, order, re)
    if two:
        ce = ce + (FEE["spot"] + slip_bp(fl, adv, order, hre, SPOT_SLIP_MULT) if fam == "B" else
                   FEE["bin"] + slip_bp(fl, adv, order, hre))
    tr["cost_e"] = ce * cost_mult / 1e4
    tr["nlegs"] = 2 if two else 1
    return tr


def gen(D, fam, cfg, lat=1, cost_mult=1.0, order_usd=None):
    if fam == "A":
        return trades_A(D, cfg, lat, cost_mult, order_usd)
    return trades_BC(D, cfg, fam, lat, cost_mult, order_usd)


def portfolio(tr, K=K_SLOTS):
    if len(tr) == 0:
        return tr.assign(acc=np.zeros(0, bool))
    tr = tr.sort_values(["te", "prio"], ascending=[True, False]).reset_index(drop=True)
    acc = slots(tr.te.values.astype(np.int64), tr.tx.values.astype(np.int64), tr.prio.values, K)
    tr["acc"] = acc
    return tr


def daily(tr, K=K_SLOTS, L=1.0):
    """daily return series (IS start .. OOS end), trade contributions L/K * net booked on the exit day (UTC)."""
    a = tr[tr.acc] if "acc" in tr else tr
    day = (a.tx.values.astype(np.int64) * 60 // 86400) - DAY0
    ok = (day >= 0) & (day < DAY_END - DAY0)
    r = np.bincount(day[ok], weights=(L / K) * a.net.values[ok], minlength=DAY_END - DAY0)
    idx = pd.date_range(IS0, periods=DAY_END - DAY0, freq="D")
    return pd.Series(r, idx)


def sharpe(r):
    s = r.std()
    return float(r.mean() / s * np.sqrt(365)) if s > 0 else 0.0


def stats(tr, K=K_SLOTS):
    r = daily(tr, K)
    a = tr[tr.acc] if "acc" in tr else tr
    tday = pd.to_datetime(a.te.values.astype(np.int64) * 60, unit="s", utc=True)
    isa = (tday >= IS0) & (tday < IS1); oosa = (tday >= OOS0) & (tday < OOS1)
    ris, roos = r[:IS1 - pd.Timedelta(days=1)], r[OOS0:]
    r25, r26 = r["2025-01-01":"2025-12-31"], r["2026-01-01":"2026-08-31"]
    def cagr(x):
        eq = float(np.prod(1 + x.values))
        yrs = len(x) / 365.0
        return eq ** (1 / yrs) - 1 if eq > 0 else -1.0
    return dict(n_is=int(isa.sum()), n_oos=int(oosa.sum()),
                bp_is=float(a.net[isa].mean() * 1e4) if isa.any() else np.nan,
                bp_oos=float(a.net[oosa].mean() * 1e4) if oosa.any() else np.nan,
                gross_bp_is=float(a.gross[isa].mean() * 1e4) if isa.any() else np.nan,
                gross_bp_oos=float(a.gross[oosa].mean() * 1e4) if oosa.any() else np.nan,
                sh_is=sharpe(ris), sh_oos=sharpe(roos), cagr_is=cagr(ris), cagr_oos=cagr(roos),
                ret25=float(np.prod(1 + r25.values) - 1), ret26=float(np.prod(1 + r26.values) - 1),
                kelly_half_is=float(0.5 * ris.mean() / ris.var()) if ris.var() > 0 else 0.0,
                n_rej=int((~tr.acc).sum()) if "acc" in tr else 0)
