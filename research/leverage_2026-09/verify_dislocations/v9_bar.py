"""Independent re-application of the pre-registered bar to the three IS picks, using the researcher's kernels.
Writes v9_bar.json (does not touch the researcher's out/)."""
import sys, json, numpy as np, pandas as pd, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
import core, s3_eval
D = core.Data()
sel = json.load(open(f"{core.W}/out/selection.json"))
LEVS = [1, 2, 3, 5, 8, 10, 15, 20]
res = {}
for fam, c in sel["per_family"].items():
    f, cfg = s3_eval.parse(c)
    tr = core.portfolio(core.gen(D, f, cfg, lat=1)); tr.attrs["fam"] = f
    r1 = s3_eval.run_sim(D, tr, 1.0, lots=False)
    eq = r1["eq"]; dr = eq.pct_change().fillna(eq.iloc[0] - 1)
    ris, roos = dr[:"2024-12-31"], dr["2025-01-01":]
    o = dict(cid=c, mtm_sh_is=core.sharpe(ris), mtm_sh_oos=core.sharpe(roos),
             ret25=float(eq["2025-12-31"] / eq["2024-12-31"] - 1), ret26=float(eq["2026-08-31"] / eq["2025-12-31"] - 1),
             hk_is=float(0.5 * ris.mean() / ris.var()))
    o["lomo_min"] = min(core.sharpe(roos[roos.index.to_period("M") != m]) for m in roos.index.to_period("M").unique())
    allt = core.gen(D, f, cfg, lat=1)
    a = tr[tr.acc]; oc = a[a.te * 60 >= core.OOS0.value // 10**9].coin.unique()
    o["loco_min"] = min(core.sharpe(core.daily(core.portfolio(allt[allt.coin != cc]))["2025-01-01":]) for cc in oc)
    o["stress_sh_oos"] = core.stats(core.portfolio(core.gen(D, f, cfg, lat=2, cost_mult=1.5)))["sh_oos"]
    lev = []
    for L in LEVS:
        trL = core.portfolio(core.gen(D, f, cfg, lat=1, order_usd=L * core.ACCOUNT / core.K_SLOTS)); trL.attrs["fam"] = f
        ro = s3_eval.intrabar_mdd(D, trL, L, core.OOS0, core.OOS1)
        lev.append(dict(L=L, mdd=ro["mdd"], liq=bool(ro["liq"]), liq_t=str(pd.Timestamp(ro["liq_t"] * 60, unit="s", tz="UTC")) if ro["liq"] else ""))
    o["leverage"] = lev
    ok = [x["L"] for x in lev if x["mdd"] >= -0.35 and not x["liq"] and x["L"] <= o["hk_is"]]
    o["supportable_L"] = max(ok) if ok else 0
    o["bar1"] = o["mtm_sh_oos"] >= 1.5 and o["ret25"] > 0 and o["ret26"] > 0
    o["bar2"] = o["lomo_min"] >= 1.0 and o["loco_min"] >= 1.0 and o["stress_sh_oos"] >= 0.8
    res[fam] = o
    print(fam, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in o.items() if k != "leverage"})
    print("   lev:", [(x["L"], round(x["mdd"], 3), x["liq"], x["liq_t"][:16]) for x in lev])
json.dump(res, open("v9_bar.json", "w"), indent=1, default=float)
