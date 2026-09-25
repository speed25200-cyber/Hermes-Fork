"""Step 2: run the whole pre-registered grid (base execution: lat=1 (60 s), costs x1, K=5, A=5000 USDT),
save per-config IS/OOS stats and daily returns; select per family on IS only.
Usage: python s2_grid.py [--is-only]   (--is-only prints IS columns only; used for smoke tests)"""
import itertools, json, sys, time
import numpy as np, pandas as pd
import core

W = core.W


def configs():
    P = json.load(open(f"{W}/prereg.json"))["grid"]
    out = []
    g = P["A_fade"]
    for u, w, k, a, v, gate, side, H, s in itertools.product(g["univ"], g["w"], g["k"], g["a_min_move"], g["vr_min"],
                                                             g["gate"], g["side"], g["H_min"], g["stop_x_move"]):
        out.append(("A", dict(univ=u, w=w, k=k, a=a, vr=v, gate=gate, side=side, H=H, s=s)))
    for fam, key, modes in (("B", "B_basis", ("hedged", "perp_only")), ("C", "C_xvenue", ("two_leg", "okx_only"))):
        g = P[key]
        for u, k, gate, mode, side, ex, H in itertools.product(g["univ"], g["k_bp"], g["gate"], modes, g["side"],
                                                               ("full", "half"), g["H_max"]):
            sd = "both" if side == "both" else "one"
            out.append((fam, dict(univ=u, k=k, gate=gate, mode=mode, side=sd, exit=ex, H=H)))
    return out


def cid(fam, cfg):
    return fam + "|" + "|".join(f"{k}={v}" for k, v in cfg.items())


def main():
    is_only = "--is-only" in sys.argv
    t0 = time.time()
    D = core.Data()
    print("loaded", len(D.syms), "coins", len(D.T), "trigger rows", len(D.Wt), "window minutes",
          round(time.time() - t0), "s", flush=True)
    rows, rets = [], {}
    cf = configs()
    print("configs", len(cf), flush=True)
    for i, (fam, cfg) in enumerate(cf):
        tr = core.portfolio(core.gen(D, fam, cfg, lat=1))
        st = core.stats(tr)
        st.update(fam=fam, cid=cid(fam, cfg), **cfg)
        rows.append(st)
        rets[cid(fam, cfg)] = core.daily(tr).values.astype(np.float32)
        if i % 250 == 0:
            print(i, round(time.time() - t0), "s", flush=True)
    G = pd.DataFrame(rows)
    if is_only:
        print(G[[c for c in G.columns if "oos" not in c and c not in ("ret25", "ret26")]].describe().T.to_string())
        return
    G.to_csv(f"{W}/out/grid_results.csv", index=False)
    R = pd.DataFrame(rets, index=pd.date_range(core.IS0, periods=core.DAY_END - core.DAY0, freq="D"))
    R.to_parquet(f"{W}/out/grid_daily_returns.parquet")
    # selection on IS only
    sel = {}
    for fam in ("A", "B", "C"):
        g = G[(G.fam == fam) & (G.n_is >= 30)]
        best = g.sort_values("sh_is", ascending=False).iloc[0]
        sel[fam] = best.cid
    over = max(sel.values(), key=lambda c: float(G.set_index("cid").loc[c, "sh_is"]))
    json.dump({"per_family": sel, "overall": over}, open(f"{W}/out/selection.json", "w"), indent=1)
    print(json.dumps({"per_family": sel, "overall": over}, indent=1))
    print("done", round(time.time() - t0), "s")


if __name__ == "__main__":
    import os
    os.makedirs(f"{W}/out", exist_ok=True)
    main()
