"""Step 7: walk-forward LightGBM ranker (pre-registered fixed parameters). One model per (universe, R), retrained
monthly on all past rows whose label window has closed (purge R hours); first prediction 2022-07-01.
Predictions -> same portfolio construction as the grid; IS measured 2022-07..2024-12, OOS 2025-01..2026-08."""
import itertools, time, sys, os
import numpy as np, pandas as pd, lightgbm as lgb
from lt_core import *

D = Data()
PARAMS = dict(objective="regression", num_leaves=15, learning_rate=0.05, min_data_in_leaf=1000, feature_fraction=0.8,
              bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, verbose=-1, num_threads=3, seed=7)
NTREES = 300
LG0 = pd.Timestamp("2022-07-01", tz="UTC")
univs = sys.argv[1].split(",") if len(sys.argv) > 1 else list(UNIV)
Rs = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [4, 8, 24]
out_rows, rets = [], {}
t_start = time.time()
for univ in univs:
    for R in Rs:
        rows_all = D.rows(R, D.dec_time[0], OOS1)
        e = D.elig(univ, rows_all)
        feats = []
        for s in SIGS:
            feats.append(pct_rank(D.f[s][rows_all], e & np.isfinite(D.f[s][rows_all])))
        feats.append(pct_rank(np.log(D.f["adv"][rows_all].astype(np.float64)), e))
        feats.append(pct_rank(D.f["vol"][rows_all], e))
        X = np.stack(feats, axis=2)  # rows x N x F
        fw = D.f[f"fwd{R}"][rows_all]
        y = pct_rank(fw, e & np.isfinite(fw))
        t = D.dec_time[rows_all]
        pred = np.full((len(rows_all), D.N), np.nan)
        months = pd.date_range(LG0, OOS1, freq="MS")
        pf = f"{W}/out/lgbm_pred_{univ}_{R}.npy"
        if os.path.exists(pf):  # re-simulation only (simulator update): reuse the walk-forward predictions
            pred = np.load(pf); months = months[:1]
        for m0, m1 in zip(months[:-1], months[1:]):
            tr = np.where(t <= m0 - pd.Timedelta(hours=R))[0]
            te = np.where((t >= m0) & (t < m1))[0]
            if len(te) == 0:
                continue
            Xtr = X[tr][e[tr] & np.isfinite(y[tr])]
            ytr = y[tr][e[tr] & np.isfinite(y[tr])]
            model = lgb.train(PARAMS, lgb.Dataset(Xtr, ytr - 0.5), NTREES)
            for i in te:
                j = np.where(e[i])[0]
                if len(j):
                    pred[i, j] = model.predict(X[i, j])
        np.save(pf, pred)
        score = np.where(e, pred, np.nan)
        for neutral, hyst in itertools.product(("dollar", "beta"), (False, True)):
            Wt = build_weights(score, e, D.f["vol"][rows_all], D.f["beta"][rows_all], hyst=hyst, neutral=neutral)
            rec = dict(univ=univ, R=R, signal="LGBM", neutral=neutral, hyst=int(hyst), kind="lgbm")
            for per, (t0, t1) in (("is", (LG0, IS1)), ("oos", (OOS0, OOS1))):
                msk = (t >= t0) & (t < t1)
                rr = rows_all[msk]
                res = simulate(D, rr, Wt[msk], t0, t1, univ=univ)
                mt = metrics(res)
                g = metrics(simulate(D, rr, Wt[msk], t0, t1, univ=univ, cost_mult=0.0))
                rec.update({f"{per}_sharpe": mt["sharpe"], f"{per}_cagr": mt["cagr"], f"{per}_vol": mt["vol"],
                            f"{per}_mdd": mt["mdd"], f"{per}_cost": mt["cost_frac"], f"{per}_turn": mt["turn_x"],
                            f"{per}_fund": mt["fund_frac"], f"{per}_gross_sharpe": g["sharpe"],
                            f"{per}_gross_cagr": g["cagr"], f"{per}_kelly": mt["kelly"]})
                for yy, v in mt["years"].items():
                    rec[f"{per}_y{yy}"] = v
                rets[f"{univ}|{R}|LGBM|{neutral}|{int(hyst)}|{per}"] = daily_returns(res["eq"])
            out_rows.append(rec)
            print(rec["univ"], R, neutral, hyst, "IS %.2f (gross %.2f) OOS %.2f (gross %.2f)" % (
                rec["is_sharpe"], rec["is_gross_sharpe"], rec["oos_sharpe"], rec["oos_gross_sharpe"]), flush=True)
        # IC of predictions
        icv = ic_series(pct_rank(pred, e & np.isfinite(pred)), fw, e)
        print(univ, R, "pred IC IS %.4f OOS %.4f" % (np.nanmean(icv[(t >= LG0) & (t < IS1)]), np.nanmean(icv[t >= OOS0])),
              round(time.time() - t_start), "s", flush=True)
tag = "_".join(univs) + "_" + "_".join(map(str, Rs))
pd.DataFrame(out_rows).to_csv(f"{W}/out/grid_lgbm_{tag}.csv", index=False)
pd.DataFrame(rets).to_parquet(f"{W}/out/grid_lgbm_returns_{tag}.parquet")
