"""EXPLORATORY (not pre-registered, defined after the OOS grid distribution was seen): the slice of family A with the
absolute cascade gate ('casc'). Equal-weight ensemble of all configs in the slice (daily returns averaged), plus the
same slice under stress (lat 2 + costs x1.5) and lat0; concentration by day; per-config OOS distribution.
Not used for the success bar."""
import json, warnings
import numpy as np, pandas as pd
import core, s2_grid
warnings.filterwarnings("ignore")
W = core.W
G = pd.read_csv(f"{W}/out/grid_results.csv")
R = pd.read_parquet(f"{W}/out/grid_daily_returns.parquet")
out = {}
for side in ("both_sides", "long_only"):
    m = (G.fam == "A") & (G.gate == "casc") & ((G.side == "long_only") if side == "long_only" else True)
    g = G[m]
    r = R[g.cid.values].mean(axis=1)
    roos = r["2025-01-01":]
    top = roos.sort_values(ascending=False)
    out[side] = dict(n_cfg=int(len(g)), cfg_oos_sh_q=[float(x) for x in np.quantile(g.sh_oos, [.1, .25, .5, .75, .9])],
                     cfg_frac_oos_pos=float((g.sh_oos > 0).mean()), cfg_frac_oos_ge1=float((g.sh_oos >= 1).mean()),
                     cfg_frac_oos_ge15_both=float(((g.sh_oos >= 1.5) & (g.ret25 > 0) & (g.ret26 > 0)).mean()),
                     ens_sh_is=core.sharpe(r[:"2024-12-31"]), ens_sh_oos=core.sharpe(roos),
                     ens_r25=float(np.prod(1 + r["2025"].values) - 1), ens_r26=float(np.prod(1 + r["2026"].values) - 1),
                     ens_oos_sh_without_2025_10_10=core.sharpe(roos.drop(pd.Timestamp("2025-10-10", tz="UTC"))),
                     ens_oos_share_top1_day=float(top.iloc[0] / roos.sum()), ens_oos_share_top5_days=float(top.iloc[:5].sum() / roos.sum()),
                     top_days={str(k.date()): float(v) for k, v in top.head(6).items()},
                     active_days_oos=int((roos != 0).sum()), active_days_is=int((r[:"2024-12-31"] != 0).sum()))
# stress / lat0 re-runs of the slice (long_only + both)
D = core.Data()
for tag, lat, cm in (("stress_lat2_cost1.5", 2, 1.5), ("lat0", 0, 1.0)):
    rr = {}
    for fam, cfg in s2_grid.configs():
        if fam == "A" and cfg["gate"] == "casc":
            tr = core.portfolio(core.gen(D, fam, cfg, lat=lat, cost_mult=cm))
            rr[s2_grid.cid(fam, cfg)] = core.daily(tr).values
    Rs = pd.DataFrame(rr, index=R.index)
    for side in ("both_sides", "long_only"):
        cols = [c for c in Rs.columns if side == "both_sides" or "side=long_only" in c]
        r = Rs[cols].mean(axis=1)
        out[side][tag] = dict(ens_sh_is=core.sharpe(r[:"2024-12-31"]), ens_sh_oos=core.sharpe(r["2025-01-01":]),
                              ens_r25=float(np.prod(1 + r["2025"].values) - 1), ens_r26=float(np.prod(1 + r["2026"].values) - 1))
json.dump(out, open(f"{W}/out/explore_casc.json", "w"), indent=1)
print(json.dumps(out, indent=1))
