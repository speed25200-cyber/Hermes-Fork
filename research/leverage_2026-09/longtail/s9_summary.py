"""Step 9: whole-grid distribution (IS and OOS, net and gross) for TAIL vs TOP30."""
import glob
import numpy as np, pandas as pd

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"
G = pd.concat([pd.read_csv(f"{W}/out/grid_singles_combos.csv")] + [pd.read_csv(f) for f in glob.glob(f"{W}/out/grid_lgbm_*.csv")],
              ignore_index=True)
G.to_csv(f"{W}/out/grid_all.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 400)


def dist(x):
    x = x.dropna()
    return pd.Series(dict(n=len(x), mean=x.mean(), p10=x.quantile(.1), p25=x.quantile(.25), med=x.median(), p75=x.quantile(.75),
                          p90=x.quantile(.9), max=x.max(), frac_pos=(x > 0).mean(), frac_ge1_5=(x >= 1.5).mean()))


rows = []
for univ, g in G.groupby("univ"):
    for col in ["is_gross_sharpe", "is_sharpe", "oos_gross_sharpe", "oos_sharpe"]:
        d = dist(g[col]); d["univ"] = univ; d["metric"] = col; rows.append(d)
S = pd.DataFrame(rows).set_index(["univ", "metric"])
print(S.round(2).to_string())
S.round(4).to_csv(f"{W}/out/grid_distribution.csv")
print()
print("IS->OOS rank correlation (net Sharpe):", {u: round(g[["is_sharpe", "oos_sharpe"]].corr("spearman").iloc[0, 1], 3) for u, g in G.groupby("univ")})
print()
k = ["univ", "R", "signal", "neutral", "hyst", "is_gross_sharpe", "is_sharpe", "oos_gross_sharpe", "oos_sharpe", "oos_y2025", "oos_y2026",
     "is_turn", "is_cost", "oos_cost", "oos_vol", "oos_mdd"]
for univ, g in G.groupby("univ"):
    print(univ, "top 15 by IS net Sharpe")
    print(g.sort_values("is_sharpe", ascending=False)[k].head(15).round(2).to_string(index=False))
    print(univ, "by R (median over configs)")
    print(g.groupby("R")[["is_gross_sharpe", "is_sharpe", "oos_gross_sharpe", "oos_sharpe", "is_turn"]].median().round(2).to_string())
    print(univ, "singles by signal (median over R/neutral/hyst)")
    s = g[g.kind == "single"].groupby("signal")[["is_gross_sharpe", "is_sharpe", "oos_gross_sharpe", "oos_sharpe", "is_turn"]].median()
    print(s.sort_values("is_sharpe", ascending=False).round(2).to_string())
    print()

# post-hoc maker grid distribution
M = pd.read_csv(f"{W}/out/grid_maker.csv")
rows = []
for univ, g in M.groupby("univ"):
    for col in ["is_sharpe", "oos_sharpe"]:
        d = dist(g[col]); d["univ"] = univ; d["metric"] = "maker_" + col; rows.append(d)
SM = pd.DataFrame(rows).set_index(["univ", "metric"])
print(SM.round(2).to_string())
SM.round(4).to_csv(f"{W}/out/grid_maker_distribution.csv")
# realised average cost per side (bp) = annual cost / annual turnover
for univ, g in G.groupby("univ"):
    print(univ, "taker: median realised cost per side (bp): IS %.1f OOS %.1f" % (1e4 * (g.is_cost / g.is_turn).median(), 1e4 * (g.oos_cost / g.oos_turn).median()))
for univ, g in M.groupby("univ"):
    print(univ, "maker: median realised cost per side (bp): OOS %.1f, median fill rate OOS %.2f" % (1e4 * (g.oos_cost / g.oos_turn).median(), g.oos_fill.median()))
