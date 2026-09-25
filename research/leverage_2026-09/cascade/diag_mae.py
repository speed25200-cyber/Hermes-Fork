import glob, os, sys, numpy as np, pandas as pd, core
syms = sorted(os.path.basename(f).replace(".cov.json", "") for f in glob.glob(f"{core.W}/data/coins/*.cov.json"))[:90]
D = core.Data(syms)
def mae(tr, fam):
    out = []
    for r in tr[tr.acc].itertuples():
        k0 = r.ei; k1 = r.ei + (r.tx - r.te)
        lc = D.Wpc[k0:k1+1] * 1e-5
        lo = lc + D.Wpl[k0:k1+1] * 1e-4; hi = lc + D.Wph[k0:k1+1] * 1e-4
        adv = (lo if r.d == 1 else hi)[1:]
        p = r.d * (np.exp(adv - r.pe) - 1)
        h = np.zeros_like(p)
        if r.nlegs == 2:
            hc = (D.Wsc if fam == "B" else D.Wbc)[k0:k1+1]; hh = (D.Wsh if fam == "B" else D.Wbh)[k0:k1+1]; hl = (D.Wsl if fam == "B" else D.Wbl)[k0:k1+1]
            lh = lc + hc * 1e-5; lhh = lh + hh * 1e-4; lhl = lh + hl * 1e-4
            h = -r.d * (np.exp((lhh if r.d == 1 else lhl)[1:] - r.he) - 1)
        out.append((p + h).min() if len(p) else 0)
    return np.array(out)
for fam, cfg in [("A", dict(univ="ALL", w=1, k=6, a=0.03, vr=5, gate="none", side="long_only", H=60, s="none")),
                 ("B", dict(univ="ALL", k=100, gate="mkt", mode="hedged", side="both", exit="full", H=30))]:
    tr = core.portfolio(core.gen(D, fam, cfg, lat=1))
    a = tr[tr.acc].copy(); a["mae"] = mae(tr, fam); a["sym"] = np.array(D.syms)[a.coin]
    a["dt"] = pd.to_datetime(a.te.astype(np.int64)*60, unit="s", utc=True)
    a = a[a.dt < core.IS1]
    print(fam, a.sort_values("mae").head(12)[["sym","dt","d","gross","net","mae","typ"]].to_string())
