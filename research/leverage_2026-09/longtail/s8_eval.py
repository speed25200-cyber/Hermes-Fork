"""Step 8: success-bar evaluation of the IS-selected configuration of each universe (and optional extra configs).
Selection = highest IS net Sharpe over singles + combos + LGBM (IS columns only). Then, OOS only:
 1) 1x net Sharpe, 2025 and 2026 returns;  2) LOMO (drop each OOS month), LOCO (re-simulate without each coin that
 was ever held OOS), costs x1.5 + 1 extra bar latency;  3) 1k / 5k / 10k accounts with OKX lots;
 4) leverage ladder with intrabar MDD, OKX tiered-MMR liquidation, IS half-Kelly. Plus correlation with the current book."""
import json, sys, glob
import numpy as np, pandas as pd
from lt_core import *

D = Data()
signs = json.load(open(f"{W}/out/signs_is.json"))
G = pd.read_csv(f"{W}/out/grid_singles_combos.csv")
L_ = [pd.read_csv(f) for f in glob.glob(f"{W}/out/grid_lgbm_*.csv")]
G = pd.concat([G] + L_, ignore_index=True)
M = pd.read_csv(f"{W}/out/grid_maker.csv")
LG0 = pd.Timestamp("2022-07-01", tz="UTC")


def score_for(univ, R, name, rows_all):
    if name == "LGBM":
        e = D.elig(univ, rows_all)
        pred = np.load(f"{W}/out/lgbm_pred_{univ}_{R}.npy")
        allrows = D.rows(R, D.dec_time[0], OOS1)
        pos = np.searchsorted(allrows, rows_all)
        return np.where(e, pred[pos], np.nan), e
    if name.startswith("COMBO"):
        comp = name.split(":")[1].split("+")
        e = D.elig(univ, rows_all)
        acc = np.zeros((len(rows_all), D.N))
        for s in comp:
            es = D.elig(univ, rows_all, s)
            p = pct_rank(D.f[s][rows_all], es)
            acc += np.nan_to_num((p - 0.5) * signs[f"{univ}|{R}|{s}"], nan=0.0)
        return np.where(e, acc / len(comp), np.nan), e
    e = D.elig(univ, rows_all, name)
    p = pct_rank(D.f[name][rows_all], e)
    return (p - 0.5) * signs[f"{univ}|{R}|{name}"], e


def weights_for(cfg):
    R = int(cfg["R"])
    rows_all = D.rows(R, D.dec_time[0], OOS1)
    sc, e = score_for(cfg["univ"], R, cfg["signal"], rows_all)
    Wt = build_weights(sc, e, D.f["vol"][rows_all], D.f["beta"][rows_all], hyst=bool(cfg["hyst"]), neutral=cfg["neutral"])
    return rows_all, Wt, sc, e


def SIM(cfg, rows, Wt, t0, t1, **kw):
    """taker (pre-registered) or maker (post-hoc) execution, from the config."""
    if "off" in cfg and cfg.get("off") == cfg.get("off") and cfg.get("off") is not None:
        return simulate_maker(D, rows, Wt, t0, t1, off=float(cfg["off"]), fallback=int(cfg["fallback"]), **kw)
    return simulate(D, rows, Wt, t0, t1, **kw)


def sub(rows_all, Wt, t0, t1):
    m = (D.dec_time[rows_all] >= t0) & (D.dec_time[rows_all] < t1)
    return rows_all[m], Wt[m]


def evaluate(cfg, book=None, do_loco=True):
    univ = cfg["univ"]
    rows_all, Wt, sc, e = weights_for(cfg)
    is0 = LG0 if cfg["signal"] == "LGBM" else IS0
    ri, wi = sub(rows_all, Wt, is0, IS1)
    ro, wo = sub(rows_all, Wt, OOS0, OOS1)
    out = {"cfg": {k: (v if not isinstance(v, (np.integer, np.floating)) else v.item()) for k, v in cfg.items()}}
    res_is = SIM(cfg, ri, wi, is0, IS1, univ=univ)
    m_is = metrics(res_is)
    r_is = daily_returns(res_is["eq"])
    res = SIM(cfg, ro, wo, OOS0, OOS1, univ=univ)
    m = metrics(res)
    r = daily_returns(res["eq"])
    out["is"] = m_is
    out["oos"] = m
    out["oos_gross"] = metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, cost_mult=0.0))
    out["oos_last_price_extremes"] = {k: metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, extremes="last", lev=L))[k2]
                                      for L in (1, 3) for k, k2 in ((f"L{L}_mdd_ib", "mdd_ib"), (f"L{L}_liq", "liq"))}
    # funding source check on the overlap of the OKX funding archive (2025-01-01..2025-09-07)
    okf = np.load(f"{W}/data/okx_funding_hourly.npz")
    t_end = pd.Timestamp("2025-09-08", tz="UTC")
    rr_, ww_ = sub(ro, wo, OOS0, t_end)
    mb = metrics(SIM(cfg, rr_, ww_, OOS0, t_end, univ=univ))
    Fb = D.F; D.F = okf["F"]
    mo = metrics(SIM(cfg, rr_, ww_, OOS0, t_end, univ=univ))
    D.F = Fb
    out["funding_check_2025_01_to_09"] = dict(binance_sharpe=mb["sharpe"], binance_cagr=mb["cagr"], binance_fund=mb["fund_frac"],
                                               okx_sharpe=mo["sharpe"], okx_cagr=mo["cagr"], okx_fund=mo["fund_frac"])
    # 1)
    y25, y26 = m["years"].get(2025, np.nan), m["years"].get(2026, np.nan)
    out["bar1"] = dict(sharpe=m["sharpe"], y2025=y25, y2026=y26, passed=bool(m["sharpe"] >= 1.5 and y25 > 0 and y26 > 0))
    # 2) LOMO
    months = r.index.to_period("M")
    lomo = {str(p): sharpe(r[months != p]) for p in months.unique()}
    # LOCO: coins ever held OOS
    held = np.where((np.abs(wo) > 0).any(axis=0))[0]
    loco = {}
    if do_loco:
        warm = D.dec_time[rows_all] >= pd.Timestamp("2024-11-01", tz="UTC")
        rw = rows_all[warm]
        for j in held:
            # the coin is removed from the eligible universe and the book is rebuilt without it
            e2 = e[warm].copy(); e2[:, j] = False
            W2 = build_weights(np.where(e2, sc[warm], np.nan), e2, D.f["vol"][rw], D.f["beta"][rw], hyst=bool(cfg["hyst"]),
                               neutral=cfg["neutral"])
            r2, w2 = sub(rw, W2, OOS0, OOS1)
            rr = SIM(cfg, r2, w2, OOS0, OOS1, univ=univ)
            loco[D.syms[j]] = sharpe(daily_returns(rr["eq"]))
    stress = metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, cost_mult=1.5, lat=1))
    stress_cost = metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, cost_mult=1.5))
    stress_lat = metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, lat=1))
    pc = pd.Series(res["pnl_coin"], index=D.syms)
    out["bar2"] = dict(lomo_min=min(lomo.values()), lomo_worst=min(lomo, key=lomo.get),
                       loco_min=(min(loco.values()) if loco else np.nan),
                       loco_worst=(min(loco, key=loco.get) if loco else ""), n_coins_held=int(len(held)),
                       stress_sharpe=stress["sharpe"], stress_cost_only=stress_cost["sharpe"], stress_lat_only=stress_lat["sharpe"],
                       top_coin_pnl=pc.sort_values(ascending=False).head(5).round(1).to_dict(),
                       bottom_coin_pnl=pc.sort_values().head(5).round(1).to_dict())
    out["bar2"]["passed"] = bool(out["bar2"]["lomo_min"] >= 1.0 and out["bar2"]["loco_min"] >= 1.0 and stress["sharpe"] >= 0.8)
    out["lomo"] = lomo
    out["loco"] = loco
    # 3) account sizes
    acc = {}
    for E0 in (1000.0, 5000.0, 10000.0):
        acc[int(E0)] = metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, E0=E0))["sharpe"]
    acc["no_lots"] = metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, use_lots=False))["sharpe"]
    npos = (np.abs(wo) > 0).sum(1)
    out["bar3"] = dict(sharpe_by_account=acc, median_names=float(np.median(npos)),
                       median_notional_per_name_5k=float(5000 / max(np.median(npos), 1)))
    # 4) leverage
    half_kelly = 0.5 * m_is["kelly"]
    lev = {}
    for L in (1, 2, 3, 5, 8, 10, 15, 20):
        mm = metrics(SIM(cfg, ro, wo, OOS0, OOS1, univ=univ, lev=L))
        lev[L] = dict(cagr=mm["cagr"], sharpe=mm["sharpe"], mdd=mm["mdd"], mdd_ib=mm["mdd_ib"], liq=mm["liq"],
                      ok=bool(mm["mdd_ib"] <= 0.35 and mm["liq"] == "" and L <= half_kelly))
    ok = [L for L, v in lev.items() if v["ok"]]
    out["bar4"] = dict(half_kelly_is=half_kelly, ladder=lev, supportable=(max(ok) if ok else 0))
    if book is not None:
        j = pd.concat([r.rename("s"), book.rename("b")], axis=1).dropna()
        out["corr_with_book"] = float(j.corr().iloc[0, 1])
        out["book_oos_sharpe_same_days"] = sharpe(j.b)
        for wgt in (0.5,):
            comb = wgt * j.s / j.s.std() + (1 - wgt) * j.b / j.b.std()
            out["blend_50_50_riskparity_sharpe"] = sharpe(comb)
    out["oos_daily"] = r
    out["is_daily"] = r_is
    return out


def load_book():
    b = pd.read_csv("/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv",
                    index_col=0, parse_dates=True)
    r = b["return"]
    r.index = pd.to_datetime(r.index, utc=True)
    return r


if __name__ == "__main__":
    book = load_book()
    results = {}
    sel = {}
    for univ in UNIV:
        g = G[G.univ == univ]
        sel[f"SELECTED_{univ}"] = g.sort_values("is_sharpe", ascending=False).iloc[0][["univ", "R", "signal", "neutral", "hyst", "kind", "is_sharpe"]].to_dict()
        m = M[M.univ == univ]
        sel[f"POSTHOC_MAKER_{univ}"] = m.sort_values("is_sharpe", ascending=False).iloc[0][["univ", "R", "signal", "neutral", "hyst", "off", "fallback", "is_sharpe"]].to_dict()
    extra = json.loads(sys.argv[1]) if len(sys.argv) > 1 else []
    todo = list(sel.items()) + [(f"EXTRA_{i}", c) for i, c in enumerate(extra)]
    daily = {}
    for name, cfg in todo:
        o = evaluate(cfg, book, do_loco=True)
        daily[name + "_oos"] = o.pop("oos_daily"); daily[name + "_is"] = o.pop("is_daily")
        results[name] = o
        print(name, json.dumps({k: o[k] for k in ("cfg", "bar1")}, default=str))
        print("  bar2", json.dumps(o["bar2"], default=str))
        print("  bar3", json.dumps(o["bar3"], default=str))
        print("  bar4", json.dumps(o["bar4"], default=str))
        print("  checks", json.dumps({k: o[k] for k in ("oos_last_price_extremes", "funding_check_2025_01_to_09")}, default=str))
        print("  IS", json.dumps({k: o["is"][k] for k in ("sharpe", "cagr", "vol", "mdd", "cost_frac", "turn_x", "years")}, default=str))
        print("  OOS", json.dumps({k: o["oos"][k] for k in ("sharpe", "cagr", "vol", "mdd", "mdd_ib", "cost_frac", "fund_frac", "turn_x")}, default=str),
              "gross OOS sharpe %.2f" % o["oos_gross"]["sharpe"], "corr book", o.get("corr_with_book"), flush=True)
    tag = sys.argv[2] if len(sys.argv) > 2 else "main"
    json.dump(results, open(f"{W}/out/eval_{tag}.json", "w"), indent=1, default=str)
    pd.DataFrame(daily).to_csv(f"{W}/out/eval_{tag}_daily.csv")
