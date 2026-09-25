import glob, os, time, numpy as np, pandas as pd, core, s3_eval
syms = sorted(os.path.basename(f).replace(".cov.json", "") for f in glob.glob(f"{core.W}/data/coins/*.cov.json"))[:90]
D = core.Data(syms)
for fam, cfg in [("A", dict(univ="ALL", w=1, k=6, a=0.03, vr=5, gate="none", side="long_only", H=60, s="none")),
                 ("A", dict(univ="ALL", w=5, k=10, a=0.01, vr=0, gate="mkt", side="both", H=240, s=0.5)),
                 ("B", dict(univ="ALL", k=100, gate="mkt", mode="hedged", side="both", exit="full", H=30)),
                 ("C", dict(univ="ALL", k=60, gate="coin", mode="two_leg", side="both", exit="half", H=240))]:
    tr = core.portfolio(core.gen(D, fam, cfg, lat=1)); tr.attrs["fam"] = fam
    r = core.daily(tr)[:"2024-12-31"]
    t1 = time.time(); o = s3_eval.run_sim(D, tr, 1.0, lots=False); dt = time.time() - t1
    eq = o["eq"][:"2024-12-31"]; dr = eq.pct_change().fillna(eq.iloc[0]-1)
    print(fam, "grid sh_is %.3f sum %.4f | mtm sh_is %.3f eqIS %.4f mdd %.3f liq %s rej %d  %.1fs" % (core.sharpe(r), r.sum(), core.sharpe(dr), eq.iloc[-1], o["mdd"], o["liq"], o["rej"], dt))
    for L in (5, 20):
        o = s3_eval.intrabar_mdd(D, tr, L, core.IS0, core.IS1)
        print("   L", L, "eq_end %.3f mdd %.3f liq %s rej %d" % (o["eq"][:"2024-12-31"].iloc[-1], o["mdd"], o["liq"], o["rej"]))
