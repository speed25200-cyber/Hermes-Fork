"""Coverage of the OKX 1m archive for universe coin-months (perp / spot / Binance), by year and IS/OOS."""
import glob, json, os
import pandas as pd
W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade"
U = pd.read_csv(f"{W}/data/universe.csv")
rows = []
for f in glob.glob(f"{W}/data/coins/*.cov.json"):
    s = os.path.basename(f).replace(".cov.json", "")
    c = json.load(open(f))
    for k in ("P", "S", "B"):
        for m in c["miss"][k]:
            rows.append((s, m.split(":")[0], k))
miss = pd.DataFrame(rows, columns=["sym", "month", "kind"])
for k, nm in (("P", "okx_perp"), ("S", "okx_spot"), ("B", "binance")):
    mm = set(map(tuple, miss[miss.kind == k][["sym", "month"]].values))
    U[nm + "_file"] = [(s, m) not in mm for s, m in zip(U.sym, U.month)]
U["year"] = U.month.str[:4]
U["period"] = (U.month >= "2025-01").map({True: "OOS", False: "IS"})
out = pd.concat([U.groupby("year")[["okx_perp_file", "okx_spot_file", "binance_file"]].mean(),
                 U.groupby("period")[["okx_perp_file", "okx_spot_file", "binance_file"]].mean()])
print(out.round(3).to_string())
print("coin-months:", len(U), " missing OKX perp:", int((~U.okx_perp_file).sum()))
print(U[~U.okx_perp_file].groupby("sym").size().sort_values(ascending=False).head(25).to_string())
out.to_csv(f"{W}/out/coverage.csv")
U.to_csv(f"{W}/out/universe_coverage.csv", index=False)
