import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
import core
G = pd.read_csv(f"{core.W}/out/grid_results.csv")
R = pd.read_parquet(f"{core.W}/out/grid_daily_returns.parquet")
g = G[(G.fam == "A") & (G.gate == "casc") & (G.side == "long_only")]
r = R[g.cid.values].mean(axis=1)
ro = r["2025-01-01":]
lomo = {str(m): core.sharpe(ro[ro.index.to_period("M") != m]) for m in ro.index.to_period("M").unique()}
out = dict(ens_sh_is=core.sharpe(r[:"2024-12-31"]), ens_sh_oos=core.sharpe(ro), lomo_min=min(lomo.values()), lomo_argmin=min(lomo, key=lomo.get))
# drop both 2025-10-10 and 2025-10-11
out["sh_oos_without_2025_10_10_11"] = core.sharpe(ro.drop([pd.Timestamp("2025-10-10", tz="UTC"), pd.Timestamp("2025-10-11", tz="UTC")]))
# a crude 1x drawdown on exit-day booked equity (lower bound on intrabar MDD)
eq = (1 + ro).cumprod(); out["oos_close_mdd_exitday"] = float((eq / eq.cummax() - 1).min())
# Selection-honest version: per-config IS Sharpe decides; share of casc slice configs whose IS Sharpe >= 1.5
out["slice_cfg_is_ge_1.5"] = int((g.sh_is >= 1.5).sum()); out["slice_n"] = len(g)
out["slice_cfg_oos_ge1.5_bothpos"] = int(((g.sh_oos >= 1.5) & (g.ret25 > 0) & (g.ret26 > 0)).sum())
# the slice's rank among all A 'gate' slices on IS (was the casc slice identifiable on IS?)
sl = G[G.fam == "A"].groupby(["gate", "side"]).agg(is_med=("sh_is", "median"), oos_med=("sh_oos", "median"))
print(sl.round(2).to_string())
ens = {}
for (gate, side), gg in G[G.fam == "A"].groupby(["gate", "side"]):
    rr = R[gg.cid.values].mean(axis=1)
    ens[f"{gate}|{side}"] = dict(is_=core.sharpe(rr[:"2024-12-31"]), oos=core.sharpe(rr["2025-01-01":]))
print(pd.DataFrame(ens).T.round(2).to_string())
out["slice_ensembles"] = ens
print(json.dumps({k: v for k, v in out.items() if k != "slice_ensembles"}, indent=1, default=float))
json.dump(out, open("v7_explore.json", "w"), indent=1, default=float)
