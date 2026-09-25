import glob, os, time, numpy as np, pandas as pd, core, s2_grid
syms = sorted(os.path.basename(f).replace(".cov.json", "") for f in glob.glob(f"{core.W}/data/coins/*.cov.json"))
t0 = time.time(); D = core.Data(syms); print("load", len(syms), round(time.time()-t0,1), len(D.T), len(D.Wt))
print("gate frac minutes dn/up active:", D.gdn_all.mean(), D.gup_all.mean(), "cnt median", np.median(D.gcnt[D.gcnt>0]))
cf = s2_grid.configs()
import random; random.seed(1)
for fam, cfg in random.sample(cf, 12) + [c for c in cf if c[0]=="A"][:2]:
    t1 = time.time(); tr = core.portfolio(core.gen(D, fam, cfg, lat=1)); st = core.stats(tr)
    print(fam, cfg, "n_is", st["n_is"], "bp_is %.1f gross %.1f sh_is %.2f" % (st["bp_is"], st["gross_bp_is"], st["sh_is"]), "rej", st["n_rej"], "%.2fs" % (time.time()-t1))
