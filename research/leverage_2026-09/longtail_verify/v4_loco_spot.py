"""Verifier step 4: spot-check LOCO for the TAIL pick (coins with the largest |P&L| + the reported worst, DASHUSDT)."""
import sys, json
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail")
import numpy as np, pandas as pd
from lt_core import *
import s8_eval as S8
V = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail_verify"
D = S8.D
cfg = dict(univ="TAIL", R=24, signal="m30d", neutral="dollar", hyst=1, kind="single")
rows_all, Wt, sc, e = S8.weights_for(cfg)
ro, wo = S8.sub(rows_all, Wt, OOS0, OOS1)
res = S8.SIM(cfg, ro, wo, OOS0, OOS1, univ="TAIL")
pc = pd.Series(res["pnl_coin"], index=D.syms)
cand = list(pc.abs().sort_values(ascending=False).head(12).index) + ["DASHUSDT"]
warm = D.dec_time[rows_all] >= pd.Timestamp("2024-11-01", tz="UTC")
rw = rows_all[warm]
out = {}
for s in dict.fromkeys(cand):
    j = D.syms.index(s)
    e2 = e[warm].copy(); e2[:, j] = False
    W2 = build_weights(np.where(e2, sc[warm], np.nan), e2, D.f["vol"][rw], D.f["beta"][rw], hyst=True, neutral="dollar")
    r2, w2 = S8.sub(rw, W2, OOS0, OOS1)
    out[s] = dict(pnl=round(float(pc[s]), 1), loco_sharpe=round(sharpe(daily_returns(S8.SIM(cfg, r2, w2, OOS0, OOS1, univ="TAIL")["eq"])), 3))
print(json.dumps(out, indent=0))
json.dump(out, open(f"{V}/v4_loco_spot.json", "w"), indent=1)
