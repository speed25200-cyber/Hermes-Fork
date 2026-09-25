import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
import core, s3_eval
D = core.Data()
m = json.load(open("okx_margin_instruments.json"))["data"]
mg = set(x["baseCcy"] for x in m if x["quoteCcy"] == "USDT" and x["state"] == "live")
U = pd.read_csv(f"{core.W}/data/universe.csv").drop_duplicates("sym").set_index("sym")
okc = np.array([U.loc[s, "base"] in mg for s in D.syms])
out = {}
for c in ["B|univ=ALL|k=150|gate=none|mode=hedged|side=one|exit=full|H=240",
          "B|univ=ALL|k=150|gate=coin|mode=hedged|side=one|exit=full|H=240",
          "B|univ=TAIL|k=150|gate=none|mode=hedged|side=one|exit=full|H=240"]:
    f, cfg = s3_eval.parse(c)
    allt = core.gen(D, f, cfg, lat=1)
    for tag, sub in (("all", allt), ("margin_pair_today", allt[okc[allt.coin.values]])):
        for cm, lat in ((1.0, 1), (1.5, 2)):
            s = core.stats(core.portfolio(sub.copy() if cm == 1.0 else sub.copy()))
            if cm != 1.0:
                t2 = core.gen(D, f, cfg, lat=lat, cost_mult=cm)
                t2 = t2 if tag == "all" else t2[okc[t2.coin.values]]
                s = core.stats(core.portfolio(t2))
            k = f"{c}|{tag}|lat{lat}_cost{cm}"
            out[k] = {x: s[x] for x in ("sh_is", "sh_oos", "n_is", "n_oos", "bp_is", "bp_oos", "ret25", "ret26")}
            print(k, {x: round(v, 2) for x, v in out[k].items()})
json.dump(out, open("v6_margin_subset.json", "w"), indent=1)
