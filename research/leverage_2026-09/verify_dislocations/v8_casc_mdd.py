"""Minute-level cross-margin sim (researcher's s3_eval.sim, intrabar last-price extremes, tier-1 MMR, lots at 5k)
for every config of the exploratory slice A|gate=casc|long_only: OOS restart at L in {1,2,3,5}, IS half-Kelly (MTM, 1x).
Also an 'ensemble-like' joint book: all 648 configs' trades are NOT combinable in one account (slot rule), so we report
the per-config distribution only."""
import sys, json, time, numpy as np, pandas as pd
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
import core, s2_grid, s3_eval
D = core.Data()
G = pd.read_csv(f"{core.W}/out/grid_results.csv").set_index("cid")
rows = []; t0 = time.time()
cf = [(f, c) for f, c in s2_grid.configs() if f == "A" and c["gate"] == "casc" and c["side"] == "long_only"]
for n, (fam, cfg) in enumerate(cf):
    cid = s2_grid.cid(fam, cfg)
    row = dict(cid=cid, sh_is=G.loc[cid, "sh_is"], sh_oos=G.loc[cid, "sh_oos"], ret25=G.loc[cid, "ret25"], ret26=G.loc[cid, "ret26"])
    tr = core.portfolio(core.gen(D, fam, cfg, lat=1)); tr.attrs["fam"] = fam
    r1 = s3_eval.run_sim(D, tr, 1.0, lots=False)
    if r1 is None:
        continue
    eq = r1["eq"]; dr = eq.pct_change().fillna(eq.iloc[0] - 1); ris = dr[:"2024-12-31"]
    row["hk_is"] = float(0.5 * ris.mean() / ris.var()) if ris.var() > 0 else 0.0
    row["mtm_sh_oos"] = core.sharpe(dr["2025-01-01":])
    for L in (1, 2, 3, 5):
        trL = core.portfolio(core.gen(D, fam, cfg, lat=1, order_usd=L * core.ACCOUNT / core.K_SLOTS)); trL.attrs["fam"] = fam
        ro = s3_eval.intrabar_mdd(D, trL, L, core.OOS0, core.OOS1)
        row[f"mdd{L}"] = ro["mdd"] if ro else 0.0; row[f"liq{L}"] = bool(ro["liq"]) if ro else False
    ok = [L for L in (1, 2, 3, 5) if row[f"mdd{L}"] >= -0.35 and not row[f"liq{L}"] and L <= row["hk_is"]]
    row["supportable_L_upto5"] = max(ok) if ok else 0
    rows.append(row)
    if n % 50 == 0:
        print(n, round(time.time() - t0), "s", flush=True)
X = pd.DataFrame(rows); X.to_csv("v8_casc_mdd.csv", index=False)
print("configs", len(X))
print("1x OOS intrabar MDD quantiles q10/q50/q90:", np.round(np.quantile(X.mdd1, [.1, .5, .9]), 3))
print("share with 1x OOS MDD <= 35%%: %.2f ; liquidated at 1x: %.2f, 2x: %.2f, 3x: %.2f, 5x: %.2f" % ((X.mdd1 >= -0.35).mean(), X.liq1.mean(), X.liq2.mean(), X.liq3.mean(), X.liq5.mean()))
print("supportable L distribution:", X.supportable_L_upto5.value_counts().sort_index().to_dict())
bar1 = (X.mtm_sh_oos >= 1.5) & (X.ret25 > 0) & (X.ret26 > 0)
print("configs with MTM OOS Sharpe >= 1.5 and both years > 0:", int(bar1.sum()), "; of those, supportable L >= 1:", int((bar1 & (X.supportable_L_upto5 >= 1)).sum()))
best = X.sort_values("sh_is", ascending=False).iloc[0]
print("slice IS-best:", best.cid, best[["sh_is", "sh_oos", "mtm_sh_oos", "ret25", "ret26", "mdd1", "mdd2", "liq2", "hk_is", "supportable_L_upto5"]].to_dict())
