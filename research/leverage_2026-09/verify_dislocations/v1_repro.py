import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
import core, s2_grid, s3_eval
G = pd.read_csv(f"{core.W}/out/grid_results.csv")
# selection check
for fam in "ABC":
    g = G[(G.fam == fam) & (G.n_is >= 30)].sort_values("sh_is", ascending=False)
    print(fam, "top3 IS:", g[["cid","sh_is","sh_oos","n_is","n_oos","ret25","ret26"]].head(3).to_string())
sel = json.load(open(f"{core.W}/out/selection.json"))
D = core.Data()
out = {}
for fam, c in sel["per_family"].items():
    f, cfg = s3_eval.parse(c)
    tr = core.portfolio(core.gen(D, f, cfg, lat=1))
    st = core.stats(tr)
    g = G.set_index("cid").loc[c]
    print(fam, "repro sh_is %.3f (grid %.3f) sh_oos %.3f (grid %.3f) n_oos %d r25 %.4f r26 %.4f" % (st["sh_is"], g.sh_is, st["sh_oos"], g.sh_oos, st["n_oos"], st["ret25"], st["ret26"]))
    out[fam] = st
json.dump(out, open("v1_repro.json", "w"), indent=1, default=float)
