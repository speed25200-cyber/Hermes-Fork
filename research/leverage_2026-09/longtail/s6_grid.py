"""Step 6: pre-registered grid. (a) IS rank ICs -> signs; (b) single-signal books; (c) COMBOk rank averages
(k best singles by IS net Sharpe). Every config is simulated separately on IS (2022-01..2024-12) and OOS
(2025-01..2026-08), each from 5,000 USDT at 1x with OKX lots; also gross (zero cost) for the tail-vs-top30 check.
Selection uses IS columns only."""
import json, itertools, time
import numpy as np, pandas as pd
from lt_core import *

D = Data()
ALL0, ALL1 = IS0, OOS1
out_rows, rets = [], {}
ic_rows = []
PCT = {}


def run_cfg(univ, R, name, score_all, rows_all, e_all, neutral, hyst, tag):
    Wt = build_weights(score_all, e_all, D.f["vol"][rows_all], D.f["beta"][rows_all], hyst=hyst, neutral=neutral)
    rec = dict(univ=univ, R=R, signal=name, neutral=neutral, hyst=int(hyst), kind=tag)
    for per, (t0, t1) in (("is", (IS0, IS1)), ("oos", (OOS0, OOS1))):
        m = (D.dec_time[rows_all] >= t0) & (D.dec_time[rows_all] < t1)
        rr = rows_all[m]
        res = simulate(D, rr, Wt[m], t0, t1, univ=univ)
        mt = metrics(res)
        g = metrics(simulate(D, rr, Wt[m], t0, t1, univ=univ, cost_mult=0.0))
        rec.update({f"{per}_sharpe": mt["sharpe"], f"{per}_cagr": mt["cagr"], f"{per}_vol": mt["vol"],
                    f"{per}_mdd": mt["mdd"], f"{per}_cost": mt["cost_frac"], f"{per}_turn": mt["turn_x"],
                    f"{per}_fund": mt["fund_frac"], f"{per}_gross_sharpe": g["sharpe"], f"{per}_gross_cagr": g["cagr"],
                    f"{per}_kelly": mt["kelly"]})
        for y, v in mt["years"].items():
            rec[f"{per}_y{y}"] = v
        key = f"{univ}|{R}|{name}|{neutral}|{hyst}|{per}"
        rets[key] = daily_returns(res["eq"])
    out_rows.append(rec)
    return rec


t_start = time.time()
signs = {}
for univ in UNIV:
    for R in (4, 8, 24):
        rows_all = D.rows(R, ALL0, ALL1)
        is_m = D.dec_time[rows_all] < IS1
        for s in SIGS:
            e = D.elig(univ, rows_all, s)
            p = pct_rank(D.f[s][rows_all], e)
            PCT[(univ, R, s)] = (p, e)
            ic = ic_series(p, D.f[f"fwd{R}"][rows_all], e)
            ic_is, ic_oos = ic[is_m], ic[~is_m]
            n_is = np.isfinite(ic_is).sum()
            # non-overlapping t-stat: IC series on the R grid with R-hour forward returns do not overlap
            t_is = np.nanmean(ic_is) / np.nanstd(ic_is) * np.sqrt(n_is)
            sg = 1.0 if np.nanmean(ic_is) >= 0 else -1.0
            signs[(univ, R, s)] = sg
            ic_rows.append(dict(univ=univ, R=R, signal=s, ic_is=np.nanmean(ic_is), t_is=t_is, sign=sg,
                                ic_oos=np.nanmean(ic_oos), t_oos=np.nanmean(ic_oos) / np.nanstd(ic_oos) * np.sqrt(np.isfinite(ic_oos).sum())))
pd.DataFrame(ic_rows).to_csv(f"{W}/out/ic_table.csv", index=False)
print("ICs done", round(time.time() - t_start), "s", flush=True)

for univ in UNIV:
    for R in (4, 8, 24):
        rows_all = D.rows(R, ALL0, ALL1)
        for s in SIGS:
            p, e = PCT[(univ, R, s)]
            score = (p - 0.5) * signs[(univ, R, s)]
            for neutral, hyst in itertools.product(("dollar", "beta"), (False, True)):
                rec = run_cfg(univ, R, s, score, rows_all, e, neutral, hyst, "single")
        print(univ, R, "singles done", round(time.time() - t_start), "s", flush=True)

G = pd.DataFrame(out_rows)
# combos: k best singles by IS net Sharpe for the same (univ, R, neutral, hyst)
for univ in UNIV:
    for R in (4, 8, 24):
        rows_all = D.rows(R, ALL0, ALL1)
        e_base = D.elig(univ, rows_all)
        for neutral, hyst in itertools.product(("dollar", "beta"), (False, True)):
            sub = G[(G.univ == univ) & (G.R == R) & (G.neutral == neutral) & (G.hyst == int(hyst)) & (G.kind == "single")]
            ranked = sub.sort_values("is_sharpe", ascending=False).signal.tolist()
            for k in (2, 3, 5):
                comp = ranked[:k]
                acc = np.zeros((len(rows_all), D.N));
                for s in comp:
                    p, _ = PCT[(univ, R, s)]
                    acc += np.nan_to_num((p - 0.5) * signs[(univ, R, s)], nan=0.0)
                score = np.where(e_base, acc / k, np.nan)
                rec = run_cfg(univ, R, f"COMBO{k}:" + "+".join(comp), score, rows_all, e_base, neutral, hyst, "combo")
        print(univ, R, "combos done", round(time.time() - t_start), "s", flush=True)

G = pd.DataFrame(out_rows)
G.to_csv(f"{W}/out/grid_singles_combos.csv", index=False)
pd.DataFrame(rets).to_parquet(f"{W}/out/grid_daily_returns.parquet")
json.dump({f"{k[0]}|{k[1]}|{k[2]}": v for k, v in signs.items()}, open(f"{W}/out/signs_is.json", "w"), indent=0)
print("grid rows", len(G), "total", round(time.time() - t_start), "s")
