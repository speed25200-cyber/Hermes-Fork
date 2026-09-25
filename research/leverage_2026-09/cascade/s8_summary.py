"""Step 8: one JSON with every number reported (reads out/*)."""
import json
import numpy as np, pandas as pd
W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade"
G = pd.read_csv(f"{W}/out/grid_results.csv")
sel = json.load(open(f"{W}/out/selection.json"))
ev = json.load(open(f"{W}/out/eval_selected.json"))
ex = json.load(open(f"{W}/out/explore_casc.json"))
cov = pd.read_csv(f"{W}/out/coverage.csv", index_col=0)
S = {"selection": sel, "coverage": cov.round(3).to_dict(), "grid": {}, "selected": {}, "explore_casc_NOT_PREREGISTERED": ex}
for fam, g in G.groupby("fam"):
    v = g[g.n_is >= 30]
    S["grid"][fam] = dict(n=int(len(g)), n_valid=int(len(v)),
                          sh_is_q10_50_90_max=[round(float(x), 3) for x in np.nanquantile(v.sh_is, [.1, .5, .9, 1])],
                          sh_oos_q10_50_90_max=[round(float(x), 3) for x in np.nanquantile(v.sh_oos, [.1, .5, .9, 1])],
                          n_is_pos=int((v.sh_is > 0).sum()), n_oos_pos=int((v.sh_oos > 0).sum()),
                          n_both_pos=int(((v.sh_is > 0) & (v.sh_oos > 0)).sum()),
                          n_oos_ge15_both_years=int(((v.sh_oos >= 1.5) & (v.ret25 > 0) & (v.ret26 > 0)).sum()),
                          n_is_ge15=int((v.sh_is >= 1.5).sum()), n_is_ge15_and_oos_ge15=int(((v.sh_is >= 1.5) & (v.sh_oos >= 1.5)).sum()),
                          spearman_is_oos=round(float(v[["sh_is", "sh_oos"]].corr(method="spearman").iloc[0, 1]), 3))
for fam in ("A", "B", "C"):
    try:
        t = json.load(open(f"{W}/out/ticks_latency_{fam}.json"))["summary"]
        S.setdefault("ticks", {})[fam] = {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in t.items() if "mean" in k}
    except FileNotFoundError:
        pass
for c, o in ev.items():
    m = o["mtm"]
    lev = {x["L"]: dict(oos_mdd=round(x["oos_mdd"], 3), oos_liq=x["oos_liq"], oos_ret=round(x["oos_ret"], 3)) for x in o["leverage"]}
    S["selected"][c] = dict(mtm=m, lomo_min=o["lomo_min"], loco_min=o["loco_min"], stress=o["stress_lat2_cost1.5"],
                            lat0=o["lat0_optimistic"], lat2=o["lat2"], cost15=o["cost1.5"], per_year=o["per_year"],
                            capacity=o["capacity"], leverage=lev, supportable_L=o["supportable_L"], exec=o["exec"],
                            corr_book_oos=o["corr_book_oos"], oos_top_days=o["oos_top_days"],
                            grid_trades_oos=o["grid_stats"]["n_oos"], grid_trades_is=o["grid_stats"]["n_is"],
                            bp_is=o["grid_stats"]["bp_is"], bp_oos=o["grid_stats"]["bp_oos"])
json.dump(S, open(f"{W}/out/summary.json", "w"), indent=1, default=str)
print(json.dumps(S["grid"], indent=1)); print(json.dumps(S.get("ticks"), indent=1))
