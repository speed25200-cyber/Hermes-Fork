"""Verifier step 1: re-run the researcher's simulator on the selected configs and reproduce the headline OOS numbers.
Also: independent vectorised P&L (pandas, no numba sim) as a cross-check of the simulator, and grid-selection checks."""
import sys, json, time
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail")
import numpy as np, pandas as pd
from lt_core import *
import s8_eval as S8

V = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail_verify"
D = S8.D
out = {}
t0 = time.time()

# --- selection check: highest IS sharpe per universe over the whole pre-registered grid
G = S8.G
for u in UNIV:
    g = G[G.univ == u].sort_values("is_sharpe", ascending=False)
    out[f"sel_{u}"] = g.iloc[0][["R", "signal", "neutral", "hyst", "is_sharpe", "oos_sharpe"]].to_dict()
    out[f"n_grid_{u}"] = int(len(g))
    out[f"oos_median_{u}"] = float(g.oos_sharpe.median())
    out[f"oos_frac_pos_{u}"] = float((g.oos_sharpe > 0).mean())
    out[f"hindsight_bar1_{u}"] = int(((g.oos_sharpe >= 1.5) & (g.oos_y2025 > 0) & (g.oos_y2026 > 0)).sum())
M = S8.M
for u in UNIV:
    m = M[M.univ == u]
    out[f"maker_n_{u}"] = int(len(m))
    out[f"maker_hindsight_bar1_{u}"] = int(((m.oos_sharpe >= 1.5) & (m.oos_y2025 > 0) & (m.oos_y2026 > 0)).sum())
    out[f"maker_oos_median_{u}"] = float(m.oos_sharpe.median())
print(json.dumps(out, default=str, indent=0), flush=True)

cfgs = {
    "TAIL": dict(univ="TAIL", R=24, signal="m30d", neutral="dollar", hyst=1, kind="single"),
    "TOP30": dict(univ="TOP30", R=24, signal="COMBO3:age+fund+r1h", neutral="beta", hyst=1, kind="combo"),
    "MAKER_TAIL": dict(univ="TAIL", R=24, signal="m3d", neutral="dollar", hyst=1, off=60, fallback=0),
}
rep = {}
for name, cfg in cfgs.items():
    rows_all, Wt, sc, e = S8.weights_for(cfg)
    ri, wi = S8.sub(rows_all, Wt, IS0, IS1)
    ro, wo = S8.sub(rows_all, Wt, OOS0, OOS1)
    mi = metrics(S8.SIM(cfg, ri, wi, IS0, IS1, univ=cfg["univ"]))
    res = S8.SIM(cfg, ro, wo, OOS0, OOS1, univ=cfg["univ"])
    mo = metrics(res)
    r = daily_returns(res["eq"])
    months = r.index.tz_localize(None).to_period("M")
    lomo = {str(p): sharpe(r[months != p]) for p in months.unique()}
    st = metrics(S8.SIM(cfg, ro, wo, OOS0, OOS1, univ=cfg["univ"], cost_mult=1.5, lat=1))
    lad = {}
    for L in (1, 2, 3, 5):
        mm = metrics(S8.SIM(cfg, ro, wo, OOS0, OOS1, univ=cfg["univ"], lev=L))
        lad[L] = dict(mdd_close_hourly=round(mm["mdd"], 4), mdd_ib=round(mm["mdd_ib"], 4), liq=mm["liq"])
    rep[name] = dict(is_sharpe=mi["sharpe"], oos_sharpe=mo["sharpe"], y=mo["years"], cagr=mo["cagr"], mdd_ib=mo["mdd_ib"],
                     lomo_min=min(lomo.values()), stress=st["sharpe"], half_kelly_is=0.5 * mi["kelly"], ladder=lad,
                     cost_frac=mo["cost_frac"], turn=mo["turn_x"])
    print(name, json.dumps(rep[name], default=str), round(time.time() - t0), "s", flush=True)
    pd.Series(r).to_csv(f"{V}/v1_{name}_oos_daily.csv")

# --- independent vectorised cross-check for the TAIL taker pick (daily rebalance at 00:00 close, 1x, no lots)
cfg = cfgs["TAIL"]
rows_all, Wt, sc, e = S8.weights_for(cfg)
ro, wo = S8.sub(rows_all, Wt, OOS0, OOS1)
K = D.K[ro]
Cf = D.Cf
F = D.F
# gross: weight x (C[k+24]/C[k]-1), funding sum over bars k+1..k+24 of w * F (approx, ignoring intra-day notional drift)
nxt = np.minimum(K + 24, Cf.shape[0] - 1)
ret = Cf[nxt] / Cf[K] - 1
cF = np.vstack([np.zeros((1, Cf.shape[1])), np.cumsum(F, axis=0)])
fsum = cF[nxt + 1] - cF[K + 1]
gross = np.nansum(wo * np.nan_to_num(ret), axis=1)
fund = np.nansum(wo * fsum, axis=1)
dw = np.abs(np.diff(np.vstack([np.zeros((1, wo.shape[1])), wo]), axis=0))
adv = np.nan_to_num(D.f["adv"][ro].astype(np.float64), nan=1e6)
usd = np.maximum(dw * 5000, 1.0)
slip = np.maximum(3.0, np.exp(COST_A + COST_B * np.log(adv) + COST_C * np.log(usd)))
cost = np.sum(dw * (FEE_BP + slip) * 1e-4, axis=1)
net = gross - fund - cost
vx = pd.Series(net, index=D.dec_time[ro])
rep["vectorised_TAIL"] = dict(sharpe_net=sharpe(net), sharpe_gross=sharpe(gross), sharpe_gross_minus_fund=sharpe(gross - fund),
                              mean_cost_per_day=float(cost.mean()), ann_cost=float(cost.mean() * 365),
                              y2025=float((1 + vx[vx.index.year == 2025]).prod() - 1),
                              y2026=float((1 + vx[vx.index.year == 2026]).prod() - 1))
print("vectorised TAIL", rep["vectorised_TAIL"], flush=True)
json.dump(dict(selection=out, repro=rep), open(f"{V}/v1_repro.json", "w"), indent=1, default=str)
