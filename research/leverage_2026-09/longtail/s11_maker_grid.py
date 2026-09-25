"""Step 11 (POST-HOC, added after the pre-registered taker grid showed that costs, not gross signal, were binding):
same single-signal books with passive execution. Grid: 12 signals x R {4,8,24} x hysteresis {0,1} x dollar-neutral x
offset {5,15,30,60} bp x fallback {cancel, taker next close}; OKX maker fee 2 bp; fills only on trade-through.
Signs are the IS-IC signs of the pre-registered grid. Selection on IS only."""
import json, itertools, time
import numpy as np, pandas as pd
from lt_core import *

D = Data()
signs = json.load(open(f"{W}/out/signs_is.json"))
rows_out, rets = [], {}
t_start = time.time()
for univ in UNIV:
    for R in (4, 8, 24):
        rows_all = D.rows(R, IS0, OOS1)
        for s in SIGS:
            e = D.elig(univ, rows_all, s)
            p = pct_rank(D.f[s][rows_all], e)
            score = (p - 0.5) * signs[f"{univ}|{R}|{s}"]
            for hyst in (False, True):
                Wt = build_weights(score, e, D.f["vol"][rows_all], D.f["beta"][rows_all], hyst=hyst, neutral="dollar")
                for off, fb in itertools.product((5, 15, 30, 60), (0, 1)):
                    rec = dict(univ=univ, R=R, signal=s, neutral="dollar", hyst=int(hyst), off=off, fallback=fb)
                    for per, (t0, t1) in (("is", (IS0, IS1)), ("oos", (OOS0, OOS1))):
                        msk = (D.dec_time[rows_all] >= t0) & (D.dec_time[rows_all] < t1)
                        res = simulate_maker(D, rows_all[msk], Wt[msk], t0, t1, off=off, fallback=fb, univ=univ)
                        mt = metrics(res)
                        rec.update({f"{per}_sharpe": mt["sharpe"], f"{per}_cagr": mt["cagr"], f"{per}_vol": mt["vol"],
                                    f"{per}_mdd": mt["mdd"], f"{per}_cost": mt["cost_frac"], f"{per}_turn": mt["turn_x"],
                                    f"{per}_fill": res["fill_rate"], f"{per}_kelly": mt["kelly"]})
                        for y, v in mt["years"].items():
                            rec[f"{per}_y{y}"] = v
                        rets[f"{univ}|{R}|{s}|{int(hyst)}|{off}|{fb}|{per}"] = daily_returns(res["eq"])
                    rows_out.append(rec)
        print(univ, R, "done", round(time.time() - t_start), "s", flush=True)
G = pd.DataFrame(rows_out)
G.to_csv(f"{W}/out/grid_maker.csv", index=False)
pd.DataFrame(rets).to_parquet(f"{W}/out/grid_maker_returns.parquet")
for univ, g in G.groupby("univ"):
    print(univ, "IS net Sharpe dist", g.is_sharpe.describe().round(2).to_dict())
    print(univ, "OOS net Sharpe dist", g.oos_sharpe.describe().round(2).to_dict(), "frac>0", round((g.oos_sharpe > 0).mean(), 3))
    k = ["R", "signal", "hyst", "off", "fallback", "is_sharpe", "is_cagr", "is_fill", "oos_sharpe", "oos_cagr", "oos_y2025", "oos_y2026", "oos_turn", "oos_cost"]
    print(g.sort_values("is_sharpe", ascending=False)[k].head(15).round(3).to_string(index=False))
