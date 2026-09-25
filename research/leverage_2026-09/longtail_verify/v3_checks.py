"""Verifier step 3: (a) OKX listing-calendar sanity vs today's live OKX instrument list;
(b) leverage ladder with close-only extremes (lower bound on intrabar risk) vs worst-case mark extremes."""
import sys, json
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail")
import numpy as np, pandas as pd
from lt_core import *
import s8_eval as S8
from hermes.execution.okx.instruments import okx_inst_id
V = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail_verify"
D = S8.D
rep = {}
lst = pd.read_parquet(f"{W}/data/okx_listed_all.parquet")
inst = pd.DataFrame(json.load(open(f"{W}/venue/okx_instruments_now.json")))
live = set(inst[(inst.state == "live") & inst.instId.str.endswith("-USDT-SWAP")].instId)
last = lst.iloc[-1]
syms = [s for s in lst.columns if s in D.syms]
a = [(s, bool(last[s]), okx_inst_id(s) in live) for s in syms]
df = pd.DataFrame(a, columns=["sym", "cal_listed_last_day", "okx_live_today"])
rep["listing_vs_today_crosstab"] = pd.crosstab(df.cal_listed_last_day, df.okx_live_today).to_dict()
rep["cal_not_listed_but_live_today"] = df[~df.cal_listed_last_day & df.okx_live_today].sym.tolist()[:20]
rep["cal_listed_but_not_live_today"] = df[df.cal_listed_last_day & ~df.okx_live_today].sym.tolist()[:20]
# tail-universe days on names never live on OKX today (delisted from OKX) -> PIT listing used, not today's list
rk = D.f["rank"]; tail = (rk >= 31) & (rk <= 150) & D.f["alive"]
notlive = np.array([okx_inst_id(s) not in live for s in D.syms])
oos = (D.dec_time >= OOS0)
rep["tail_rowfrac_on_names_not_on_okx_today_IS"] = float(tail[~oos][:, notlive].sum() / tail[~oos].sum())
rep["tail_rowfrac_on_names_not_on_okx_today_OOS"] = float(tail[oos][:, notlive].sum() / tail[oos].sum())
print(rep, flush=True)
# (b) close-only extremes
cfgs = {"TAIL": dict(univ="TAIL", R=24, signal="m30d", neutral="dollar", hyst=1, kind="single"),
        "TOP30": dict(univ="TOP30", R=24, signal="COMBO3:age+fund+r1h", neutral="beta", hyst=1, kind="combo")}
XH0, XL0 = D.XH, D.XL
for name, cfg in cfgs.items():
    rows_all, Wt, sc, e = S8.weights_for(cfg)
    ro, wo = S8.sub(rows_all, Wt, OOS0, OOS1)
    for mode in ("close_only", "mark_worstcase"):
        if mode == "close_only":
            D.XH, D.XL = D.Cf, D.Cf
        else:
            D.XH, D.XL = XH0, XL0
        for L in (1, 2, 3, 5, 8):
            m = metrics(S8.SIM(cfg, ro, wo, OOS0, OOS1, univ=cfg["univ"], lev=L))
            rep[f"{name}_{mode}_L{L}"] = dict(mdd_ib=round(m["mdd_ib"], 4), liq=m["liq"], cagr=round(m["cagr"], 4))
    D.XH, D.XL = XH0, XL0
for k, v in rep.items():
    if "_L" in k:
        print(k, v)
json.dump(rep, open(f"{V}/v3_checks.json", "w"), indent=1, default=str)
