import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
import core, s3_eval
D = core.Data()
c = "B|univ=ALL|k=150|gate=none|mode=hedged|side=one|exit=full|H=240"
f, cfg = s3_eval.parse(c)
out = {}
for name, lat, cm in (("base", 1, 1.0), ("lat0", 0, 1.0), ("lat2", 2, 1.0), ("lat3", 3, 1.0), ("cost1.5", 1, 1.5), ("stress_lat2_cost1.5", 2, 1.5)):
    s = core.stats(core.portfolio(core.gen(D, f, cfg, lat=lat, cost_mult=cm)))
    out[name] = {k: s[k] for k in ("sh_is", "sh_oos", "bp_is", "bp_oos", "n_is", "n_oos", "ret25", "ret26")}
    print(name, {k: round(v, 3) for k, v in out[name].items()})
# spot-leg borrow cost sensitivity: flat extra borrow cost per short-spot trade (bp)
tr = core.portfolio(core.gen(D, f, cfg, lat=1))
for extra in (10, 25, 50):
    t2 = tr.copy(); t2["net"] = t2.net - extra / 1e4
    s = core.stats(t2); print("extra borrow %d bp/trade: sh_is %.2f sh_oos %.2f" % (extra, s["sh_is"], s["sh_oos"]))
    out[f"borrow_plus_{extra}bp"] = dict(sh_is=s["sh_is"], sh_oos=s["sh_oos"])
# LOMO / LOCO
r = core.daily(tr)["2025-01-01":]
lomo = {str(m): core.sharpe(r[r.index.to_period("M") != m]) for m in r.index.to_period("M").unique()}
a = tr[tr.acc]; oos_coins = a[a.te * 60 >= core.OOS0.value // 10**9].coin.unique()
allt = core.gen(D, f, cfg, lat=1)
loco = {D.syms[cc]: core.sharpe(core.daily(core.portfolio(allt[allt.coin != cc]))["2025-01-01":]) for cc in oos_coins}
print("LOMO min %.2f LOCO min %.2f (%s)" % (min(lomo.values()), min(loco.values()), min(loco, key=loco.get)))
out["lomo_min"] = min(lomo.values()); out["loco_min"] = min(loco.values())
# minute-level MTM sim with margin (2-leg, spot MMR 10%, 5x)
tr.attrs["fam"] = "B"
for L in (1, 2, 3, 5):
    trL = core.portfolio(core.gen(D, f, cfg, lat=1, order_usd=L * core.ACCOUNT / core.K_SLOTS)); trL.attrs["fam"] = "B"
    ro = s3_eval.intrabar_mdd(D, trL, L, core.OOS0, core.OOS1)
    ri = s3_eval.intrabar_mdd(D, trL, L, core.IS0, core.IS1)
    eo = ro["eq"]["2025-01-01":]; do = eo.pct_change().fillna(eo.iloc[0] - 1)
    ei = ri["eq"][:"2024-12-31"]; di = ei.pct_change().fillna(ei.iloc[0] - 1)
    print("L=%d OOS sh %.2f mdd %.3f liq %s | IS sh %.2f mdd %.3f half-kelly_IS(1x) %.1f" % (L, core.sharpe(do), ro["mdd"], ro["liq"], core.sharpe(di), ri["mdd"], 0.5 * di.mean() / di.var() / L))
    out[f"L{L}"] = dict(oos_sh=core.sharpe(do), oos_mdd=ro["mdd"], liq=ro["liq"], is_sh=core.sharpe(di), is_mdd=ri["mdd"])
json.dump(out, open("v5_hedged_stress.json", "w"), indent=1, default=float)
