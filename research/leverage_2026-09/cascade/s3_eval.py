"""Step 3: evaluate the IS-selected configs: minute-level mark-to-market account simulation with intrabar extremes,
OKX tier-1 MMR, instrument max leverage, lot sizes; leverage table; robustness (LOMO, LOCO, costs x1.5 + 1 bar);
latency references; executability for 1k/10k accounts; event frequency; correlation with the current book.
Usage: python s3_eval.py [cid ...]   (default: the per-family selections in out/selection.json)"""
import json, sys, time
import numpy as np, pandas as pd
from numba import njit
import core, s2_grid

W = core.W
LEVS = [1, 2, 3, 5, 8, 10, 15, 20]
SPOT_MMR, SPOT_LEVER = 0.10, 5.0          # short/long spot leg on OKX margin (conservative)
BIN_MMR, BIN_LEVER = 0.01, 20.0           # Binance alt perps tier 1 (typical), separate account


def parse(c):
    p = c.split("|")
    fam = p[0]
    cfg = {}
    for kv in p[1:]:
        k, v = kv.split("=")
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass
        cfg[k] = v
    return fam, cfg


@njit(cache=True)
def sim(te, tx, d, ei, pe, px, he, hx, nlegs, hkind, cost_e, cost_x, fund, mmr, lever, lot_usd, min_usd,
        Wt, Wpc, Wph, Wpl, Wsc, Wsh, Wsl, Wbc, Wbh, Wbl, L, K, acct, nday0, nday, typ):
    """Trades sorted by te (already accepted by the slot rule). Minute loop over the union of trade lives.
    Entry at the close of te (entry cost charged), marked from te+1: close MTM and joint adverse extreme (lows for
    longs, highs for shorts, hedge leg opposite); a stop exit caps the adverse price at the fill. Liquidation when
    worst equity <= sum(MMR*notional + 5 bp close fee): account ends (equity 0). Lot rounding with acct USDT."""
    n = te.shape[0]
    E = 1.0
    peak = 1.0; mdd = 0.0; liq = False; liq_t = -1
    eq_day = np.full(nday, np.nan)
    openi = np.full(16, -1, np.int64); N = np.zeros(n); Nh = np.zeros(n)
    rej = 0
    i = 0
    m = te[0] - 1 if n > 0 else 0
    while True:
        nopen = 0
        for s in range(16):
            if openi[s] >= 0:
                nopen += 1
        if nopen == 0:
            if i >= n:
                break
            m = te[i]
        else:
            m = m + 1
        # entries at the close of m
        while i < n and te[i] == m:
            imr = 0.0
            for s in range(16):
                j = openi[s]
                if j >= 0:
                    imr += N[j] / lever[j]
                    if nlegs[j] == 2:
                        imr += Nh[j] / (SPOT_LEVER if hkind[j] == 1 else BIN_LEVER)
            notional = L / K * E
            ok = True
            if notional * acct < min_usd[i]:
                ok = False
            elif lot_usd[i] > 0:
                notional = np.floor(notional * acct / lot_usd[i]) * lot_usd[i] / acct
                if notional <= 0:
                    ok = False
            nh = notional if nlegs[i] == 2 else 0.0
            if ok:
                need = imr + notional / lever[i]
                if nlegs[i] == 2:
                    need += nh / (SPOT_LEVER if hkind[i] == 1 else BIN_LEVER)
                if need > E:
                    ok = False
            if ok:
                for s in range(16):
                    if openi[s] < 0:
                        openi[s] = i
                        N[i] = notional; Nh[i] = nh
                        E -= notional * cost_e[i]
                        break
            else:
                rej += 1
            i += 1
        # mark
        worst = E; mtm = E; mm = 0.0; anyopen = False
        for s in range(16):
            j = openi[s]
            if j < 0:
                continue
            anyopen = True
            if m == te[j]:
                mm += N[j] * (mmr[j] + 0.0005)
                continue
            k = ei[j] + (m - te[j])
            if k >= Wt.shape[0] or Wt[k] != m or Wpc[k] == -2147483648:
                mm += N[j] * (mmr[j] + 0.0005)
                continue
            lc = Wpc[k] * 1e-5
            lo = lc + (Wpl[k] * 1e-4 if Wpl[k] != -32768 else 0.0)
            hi = lc + (Wph[k] * 1e-4 if Wph[k] != -32768 else 0.0)
            adv_p = lo if d[j] == 1 else hi
            if m == tx[j] and typ[j] == 1 and nlegs[j] == 1:
                adv_p = px[j]
            mtm += N[j] * d[j] * (np.exp(lc - pe[j]) - 1)
            worst += N[j] * d[j] * (np.exp(adv_p - pe[j]) - 1)
            mm += N[j] * (mmr[j] + 0.0005)
            if nlegs[j] == 2:
                if hkind[j] == 1:
                    hcl = Wsc[k]; hhi = Wsh[k]; hlo = Wsl[k]
                else:
                    hcl = Wbc[k]; hhi = Wbh[k]; hlo = Wbl[k]
                if hcl != -2147483648:
                    lh = lc + hcl * 1e-5
                    lhh = lh + (hhi * 1e-4 if hhi != -32768 else 0.0)
                    lhl = lh + (hlo * 1e-4 if hlo != -32768 else 0.0)
                    hadv = lhh if d[j] == 1 else lhl
                    mtm += Nh[j] * (-d[j]) * (np.exp(lh - he[j]) - 1)
                    worst += Nh[j] * (-d[j]) * (np.exp(hadv - he[j]) - 1)
                mm += Nh[j] * ((SPOT_MMR if hkind[j] == 1 else BIN_MMR) + 0.0005)
        if anyopen:
            if worst <= mm:
                liq = True; liq_t = m; E = 0.0
                day = (m * 60) // 86400 - nday0
                if 0 <= day < nday:
                    eq_day[day:] = 0.0
                break
            if mtm > peak:
                peak = mtm
            dd = worst / peak - 1
            if dd < mdd:
                mdd = dd
        # exits at m
        for s in range(16):
            j = openi[s]
            if j >= 0 and tx[j] == m:
                g = d[j] * (np.exp(px[j] - pe[j]) - 1)
                gh = -d[j] * (np.exp(hx[j] - he[j]) - 1) if nlegs[j] == 2 else 0.0
                E += N[j] * (g - cost_x[j] + fund[j]) + Nh[j] * gh
                openi[s] = -1
        still = False
        mtm2 = E
        for s in range(16):
            if openi[s] >= 0:
                still = True
        if not still and E > peak:
            peak = E
        day = (m * 60) // 86400 - nday0
        if 0 <= day < nday:
            eq_day[day] = mtm if still else E
    return eq_day, mdd, liq, liq_t, rej


def run_sim(D, tr, L, acct=core.ACCOUNT, lots=True):
    a = tr[tr.acc].sort_values(["te", "prio"], ascending=[True, False]).reset_index(drop=True)
    if len(a) == 0:
        return None
    meta = D.meta
    syms = np.array(D.syms)
    mmr = meta.loc[syms[a.coin.values], "mmr1"].values.astype(float)
    lever = meta.loc[syms[a.coin.values], "lever"].values.astype(float)
    ct = meta.loc[syms[a.coin.values], "ctVal"].values.astype(float)
    lot = meta.loc[syms[a.coin.values], "lotSz"].values.astype(float)
    mn = meta.loc[syms[a.coin.values], "minSz"].values.astype(float)
    price = np.exp(a.pe.values)
    lot_usd = np.where(np.isfinite(ct * lot), ct * lot * price, 1.0) if lots else np.zeros(len(a))
    min_usd = np.where(np.isfinite(ct * mn), ct * mn * price, 1.0) if lots else np.zeros(len(a))
    he = a.he.values if "he" in a else np.zeros(len(a)); hx = a.hx.values if "hx" in a else np.zeros(len(a))
    hkind = np.full(len(a), 1 if tr.attrs.get("fam") == "B" else 2, np.int64)
    cost_x = a.cost.values - a.cost_e.values
    nday0 = core.DAY0; nday = core.DAY_END - core.DAY0
    eq, mdd, liq, liq_t, rej = sim(a.te.values.astype(np.int64), a.tx.values.astype(np.int64), a.d.values.astype(np.int64),
                                   a.ei.values.astype(np.int64), a.pe.values, a.px.values, he, hx,
                                   a.nlegs.values.astype(np.int64), hkind, a.cost_e.values, cost_x, a.fund.values,
                                   mmr, lever, lot_usd, min_usd,
                                   D.Wt, D.Wpc, D.Wph, D.Wpl, D.Wsc, D.Wsh, D.Wsl, D.Wbc, D.Wbh, D.Wbl,
                                   float(L), float(core.K_SLOTS), float(acct), nday0, nday, a.typ.values.astype(np.int64))
    eq = pd.Series(eq, pd.date_range(core.IS0, periods=nday, freq="D")).ffill().fillna(1.0)
    return dict(eq=eq, mdd=mdd, liq=liq, liq_t=liq_t, rej=rej)


def seg_stats(eq, a, b):
    e = eq[a:b]
    prev = eq[:a].iloc[-2] if len(eq[:a]) > 1 else 1.0
    r = e.pct_change().fillna(e.iloc[0] / prev - 1)
    tot = float(e.iloc[-1] / prev - 1)
    yrs = len(e) / 365
    return r, tot, (1 + tot) ** (1 / yrs) - 1 if tot > -1 else -1.0


def intrabar_mdd(D, tr, L, a0, a1, acct=core.ACCOUNT):
    """MDD restricted to trades entered within [a0, a1): re-run the sim on that subset (equity restarts at 1)."""
    tday = pd.to_datetime(tr.te.values.astype(np.int64) * 60, unit="s", utc=True)
    sub = tr[(tday >= a0) & (tday < a1)]
    sub.attrs = tr.attrs
    return run_sim(D, sub, L, acct)


def main():
    t0 = time.time()
    D = core.Data()
    G = pd.read_csv(f"{W}/out/grid_results.csv")
    sel = json.load(open(f"{W}/out/selection.json"))
    cids = sys.argv[1:] or list(dict.fromkeys(list(sel["per_family"].values())))
    book = pd.read_csv("/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv")
    res = {}
    for c in cids:
        fam, cfg = parse(c)
        print("=" * 100, "\n", c, flush=True)
        out = {"cid": c, "fam": fam, "cfg": cfg}
        tr = core.portfolio(core.gen(D, fam, cfg, lat=1)); tr.attrs["fam"] = fam
        st = core.stats(tr); out["grid_stats"] = st
        # minute-level sim at L=1 (no lot constraint) -> MTM daily returns
        r1 = run_sim(D, tr, 1.0, lots=False)
        eq = r1["eq"]; dr = eq.pct_change().fillna(eq.iloc[0] - 1)
        ris, roos = dr[:"2024-12-31"], dr["2025-01-01":]
        out["mtm"] = dict(sh_is=core.sharpe(ris), sh_oos=core.sharpe(roos),
                          ret25=float(eq["2025-12-31"] / eq["2024-12-31"] - 1),
                          ret26=float(eq["2026-08-31"] / eq["2025-12-31"] - 1),
                          cagr_is=float(eq["2024-12-31"] ** (1 / 3.0) - 1),
                          cagr_oos=float((eq.iloc[-1] / eq["2024-12-31"]) ** (365 / len(roos)) - 1),
                          kelly_half_is=float(0.5 * ris.mean() / ris.var()), mdd_full=r1["mdd"])
        # per-trade and frequency
        a = tr[tr.acc].copy()
        a["dt"] = pd.to_datetime(a.te.values.astype(np.int64) * 60, unit="s", utc=True)
        a["year"] = a.dt.dt.year
        out["per_year"] = a.groupby("year").agg(n=("net", "size"), net_bp=("net", lambda x: 1e4 * x.mean()),
                                                gross_bp=("gross", lambda x: 1e4 * x.mean()),
                                                cost_bp=("cost", lambda x: 1e4 * x.mean()),
                                                win=("net", lambda x: (x > 0).mean()),
                                                days=("dt", lambda x: x.dt.date.nunique()),
                                                coins=("coin", "nunique")).reset_index().to_dict("records")
        oos = a[a.dt >= core.OOS0]
        out["oos_top_days"] = (oos.groupby(oos.dt.dt.date).net.sum() / core.K_SLOTS).sort_values(ascending=False).head(8).to_dict()
        out["oos_top_days"] = {str(k): float(v) for k, v in out["oos_top_days"].items()}
        # LOMO (OOS, MTM daily returns at 1x)
        lomo = {}
        for mth, g in roos.groupby(roos.index.to_period("M")):
            lomo[str(mth)] = core.sharpe(roos[roos.index.to_period("M") != mth])
        out["lomo_min"] = min(lomo.values()); out["lomo_argmin"] = min(lomo, key=lomo.get); out["lomo"] = lomo
        # LOCO (OOS): rerun slots without each coin that traded OOS
        loco = {}
        allt = core.gen(D, fam, cfg, lat=1)
        for cc in sorted(oos.coin.unique()):
            t2 = core.portfolio(allt[allt.coin != cc])
            r2 = core.daily(t2)["2025-01-01":]
            loco[D.syms[cc]] = core.sharpe(r2)
        out["loco_min"] = min(loco.values()) if loco else np.nan
        out["loco_argmin"] = min(loco, key=loco.get) if loco else None
        out["loco"] = loco
        # stress: costs x1.5 + one extra bar; latency references
        for name, lat, cm in (("stress_lat2_cost1.5", 2, 1.5), ("lat0_optimistic", 0, 1.0), ("lat2", 2, 1.0),
                              ("cost1.5", 1, 1.5)):
            t3 = core.portfolio(core.gen(D, fam, cfg, lat=lat, cost_mult=cm))
            s3 = core.stats(t3)
            out[name] = {k: s3[k] for k in ("sh_is", "sh_oos", "bp_is", "bp_oos", "n_is", "n_oos", "ret25", "ret26")}
        # capacity: per-trade net edge vs order size (slippage model scales with order size)
        cap = {}
        for osz in (200, 1000, 2000, 10000, 50000, 200000):
            t4 = core.portfolio(core.gen(D, fam, cfg, lat=1, order_usd=osz))
            s4 = core.stats(t4)
            cap[osz] = dict(bp_is=s4["bp_is"], bp_oos=s4["bp_oos"], sh_is=s4["sh_is"], sh_oos=s4["sh_oos"])
        out["capacity"] = cap
        # leverage table (A = 5000 USDT, lots on), OOS-only sim restarted at 2025-01-01, and IS sim
        lev = []
        for L in LEVS:
            trL = core.portfolio(core.gen(D, fam, cfg, lat=1, order_usd=L * core.ACCOUNT / core.K_SLOTS)); trL.attrs["fam"] = fam
            ro = intrabar_mdd(D, trL, L, core.OOS0, core.OOS1)
            ri = intrabar_mdd(D, trL, L, core.IS0, core.IS1)
            eo = ro["eq"]["2025-01-01":]; ei_ = ri["eq"][:"2024-12-31"]
            do = eo.pct_change().fillna(eo.iloc[0] - 1)
            lev.append(dict(L=L, oos_ret=float(eo.iloc[-1] - 1) if not ro["liq"] else -1.0,
                            oos_cagr=float(eo.iloc[-1] ** (365 / len(eo)) - 1) if not ro["liq"] else -1.0,
                            oos_mdd=ro["mdd"], oos_liq=ro["liq"],
                            oos_liq_t=str(pd.Timestamp(ro["liq_t"] * 60, unit="s", tz="UTC")) if ro["liq"] else "",
                            oos_sh=core.sharpe(do), oos_rej=ro["rej"],
                            is_cagr=float(ei_.iloc[-1] ** (1 / 3) - 1) if not ri["liq"] else -1.0, is_mdd=ri["mdd"],
                            is_liq=ri["liq"]))
        out["leverage"] = lev
        hk = out["mtm"]["kelly_half_is"]
        ok = [x["L"] for x in lev if x["oos_mdd"] >= -0.35 and not x["oos_liq"] and x["L"] <= hk]
        out["supportable_L"] = max(ok) if ok else 0
        # executability: 1k and 10k accounts at L=1 and at supportable L (lot sizes, min size)
        ex = {}
        for acct in (1000.0, 10000.0):
            for L in sorted({1, max(out["supportable_L"], 1)}):
                trL = core.portfolio(core.gen(D, fam, cfg, lat=1, order_usd=L * acct / core.K_SLOTS)); trL.attrs["fam"] = fam
                ro = intrabar_mdd(D, trL, L, core.OOS0, core.OOS1, acct=acct)
                eo = ro["eq"]["2025-01-01":]
                do = eo.pct_change().fillna(eo.iloc[0] - 1)
                ex[f"A{int(acct)}_L{L}"] = dict(oos_sh=core.sharpe(do), oos_ret=float(eo.iloc[-1] - 1), rej=ro["rej"],
                                                n=int((trL.acc & (trL.te * 60 >= core.OOS0.value // 10**9)).sum()),
                                                mdd=ro["mdd"], liq=ro["liq"])
        out["exec"] = ex
        # known OKX meta for traded coins today
        tc = [D.syms[c_] for c_ in oos.coin.unique()]
        out["oos_coins_meta_known"] = float(np.mean([bool(D.meta.loc[s, "known"]) for s in tc])) if tc else np.nan
        # correlation with the current book (OOS daily)
        b = book.copy(); b.index = pd.to_datetime(b.iloc[:, 0], utc=True)
        br = b["return"].reindex(roos.index)
        out["corr_book_oos"] = float(pd.concat([roos, br], axis=1).dropna().corr().iloc[0, 1])
        res[c] = out
        print(json.dumps({k: v for k, v in out.items() if k not in ("lomo", "loco")}, indent=1, default=str)[:6000], flush=True)
        # save the trade list of the selected config
        a.to_csv(f"{W}/out/trades_{fam}.csv", index=False)
        pd.DataFrame({"mtm_equity_1x": eq, "daily_ret_1x": dr}).to_csv(f"{W}/out/equity_{fam}.csv")
    json.dump(res, open(f"{W}/out/eval_selected.json", "w"), indent=1, default=str)
    print("done", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()
