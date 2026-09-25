"""Diagnostic: why strong rank ICs do not become dollar P&L in the tail (fat right tail of short-leg names)."""
import numpy as np, pandas as pd, json
from lt_core import *
D = Data()
signs = json.load(open(f"{W}/out/signs_is.json"))
R = 24
rows_all = D.rows(R, D.dec_time[0], OOS1)
out = []
for univ in UNIV:
    for name in ["LGBM", "r1d", "age", "vshock", "m3d"]:
        if name == "LGBM":
            try:
                pred = np.load(f"{W}/out/lgbm_pred_{univ}_{R}.npy")
            except FileNotFoundError:
                continue
            e = D.elig(univ, rows_all); sc = np.where(e, pred, np.nan)
        else:
            e = D.elig(univ, rows_all, name)
            sc = (pct_rank(D.f[name][rows_all], e) - 0.5) * signs[f"{univ}|{R}|{name}"]
        fw = np.expm1(D.f["fwd24"][rows_all].astype(np.float64))
        for per, (t0, t1) in (("IS", (pd.Timestamp("2022-07-01", tz="UTC"), IS1)), ("OOS", (OOS0, OOS1))):
            m = np.where((D.dec_time[rows_all] >= t0) & (D.dec_time[rows_all] < t1))[0]
            L, S, Lmed, Smed, Lw, Sw, big = [], [], [], [], [], [], []
            for i in m:
                ok = e[i] & np.isfinite(sc[i]) & np.isfinite(fw[i])
                j = np.where(ok)[0]
                if len(j) < 8: continue
                o = np.argsort(sc[i, j]); n = len(j); k = max(1, round(0.25 * n))
                lo, hi = j[o[:k]], j[o[-k:]]
                x = fw[i]; w = np.clip(x, np.nanpercentile(x[j], 1), np.nanpercentile(x[j], 99))
                L.append(x[hi].mean()); S.append(x[lo].mean()); Lmed.append(np.median(x[hi])); Smed.append(np.median(x[lo]))
                Lw.append(w[hi].mean()); Sw.append(w[lo].mean())
                big.append(np.clip(x[lo] - 0.3, 0, None).sum() / k)  # short-leg loss from >+30% days, per name
            L, S = np.array(L), np.array(S)
            out.append(dict(univ=univ, signal=name, period=per, days=len(L), long_mean_bp=1e4*L.mean(), short_mean_bp=1e4*S.mean(),
                            spread_mean_bp=1e4*(L-S).mean(), spread_median_names_bp=1e4*(np.array(Lmed)-np.array(Smed)).mean(),
                            spread_winsor_bp=1e4*(np.array(Lw)-np.array(Sw)).mean(), short_leg_excess_over30pct_bp=1e4*np.mean(big),
                            frac_days_spread_pos=float(((L-S) > 0).mean())))
df = pd.DataFrame(out)
pd.set_option("display.width", 250)
print(df.round(1).to_string(index=False))
df.to_csv(f"{W}/out/diag_legs_R24.csv", index=False)
