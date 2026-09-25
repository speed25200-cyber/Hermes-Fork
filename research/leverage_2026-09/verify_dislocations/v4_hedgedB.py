import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
import core, s3_eval
D = core.Data()
G = pd.read_csv(f"{core.W}/out/grid_results.csv").set_index("cid")
cids = ["B|univ=ALL|k=150|gate=none|mode=hedged|side=one|exit=full|H=240",
        "B|univ=ALL|k=150|gate=coin|mode=hedged|side=one|exit=full|H=240",
        "B|univ=ALL|k=150|gate=none|mode=perp_only|side=one|exit=full|H=240"]
for c in cids:
    f, cfg = s3_eval.parse(c)
    tr = core.portfolio(core.gen(D, f, cfg, lat=1)); a = tr[tr.acc].copy()
    a["sym"] = np.array(D.syms)[a.coin.values]
    a["dt"] = pd.to_datetime(a.te.values.astype(np.int64) * 60, unit="s", utc=True)
    a["per"] = np.where(a.dt >= core.OOS0, "OOS", "IS")
    a["hold"] = a.tx - a.te
    print("=" * 30, c, G.loc[c, ["sh_is", "sh_oos", "n_is", "n_oos"]].to_dict())
    print(a.groupby("per").agg(n=("net", "size"), net_bp=("net", lambda x: 1e4*x.mean()), perp_bp=("perp_leg", lambda x: 1e4*x.mean()),
                               hedge_bp=("hedge_leg", lambda x: 1e4*x.mean()), cost_bp=("cost", lambda x: 1e4*x.mean()),
                               win=("net", lambda x: (x > 0).mean()), hold_med=("hold", "median"), coins=("sym", "nunique")).round(1).to_string())
    o = a[a.per == "OOS"]
    by = o.groupby("sym").agg(n=("net", "size"), sum_net=("net", "sum"), perp=("perp_leg", "sum"), hedge=("hedge_leg", "sum")).sort_values("sum_net", ascending=False)
    print("OOS by coin (top 10):"); print(by.head(10).round(3).to_string())
    print("share of OOS net from top coin %.2f top3 %.2f" % (by.sum_net.iloc[0] / by.sum_net.sum(), by.sum_net.iloc[:3].sum() / by.sum_net.sum()))
    i = a[a.per == "IS"]
    byi = i.groupby("sym").agg(n=("net", "size"), sum_net=("net", "sum")).sort_values("sum_net", ascending=False)
    print("IS by coin (top 5):"); print(byi.head(5).round(3).to_string())
    a.to_csv(f"v4_{'_'.join(x.split('=')[-1] for x in c.split('|')[1:])}.csv", index=False)
