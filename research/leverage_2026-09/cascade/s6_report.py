"""Step 6: grid distribution tables + success-bar verdict for the IS-selected configs (reads out/*)."""
import json
import numpy as np, pandas as pd

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade"
G = pd.read_csv(f"{W}/out/grid_results.csv")
sel = json.load(open(f"{W}/out/selection.json"))
ev = json.load(open(f"{W}/out/eval_selected.json"))
lines = []
P = lambda *a: lines.append(" ".join(str(x) for x in a))

P("GRID DISTRIBUTION (base: 60 s latency, VIP0 taker, slippage model, K=5, 1x)")
for fam, g in G.groupby("fam"):
    v = g[g.n_is >= 30]
    P(f"-- family {fam}: {len(g)} configs, {len(v)} with >=30 IS trades")
    P("   IS Sharpe  q10/q50/q90/max: %.2f / %.2f / %.2f / %.2f" % tuple(np.nanquantile(v.sh_is, [.1, .5, .9, 1])))
    P("   OOS Sharpe q10/q50/q90/max: %.2f / %.2f / %.2f / %.2f" % tuple(np.nanquantile(v.sh_oos, [.1, .5, .9, 1])))
    P("   configs IS Sharpe>0: %d, OOS>0: %d, both>0: %d; OOS>=1.5: %d; OOS>=1.5 & 2025>0 & 2026>0: %d" % (
        (v.sh_is > 0).sum(), (v.sh_oos > 0).sum(), ((v.sh_is > 0) & (v.sh_oos > 0)).sum(), (v.sh_oos >= 1.5).sum(),
        ((v.sh_oos >= 1.5) & (v.ret25 > 0) & (v.ret26 > 0)).sum()))
    P("   IS/OOS Sharpe rank corr: %.2f" % v[["sh_is", "sh_oos"]].corr(method="spearman").iloc[0, 1])
    P("   net bp/trade median IS %.1f OOS %.1f; gross median IS %.1f OOS %.1f" % (
        v.bp_is.median(), v.bp_oos.median(), v.gross_bp_is.median(), v.gross_bp_oos.median()))
    top = v.sort_values("sh_is", ascending=False).head(10)
    P(top[["cid", "n_is", "n_oos", "bp_is", "bp_oos", "sh_is", "sh_oos", "ret25", "ret26"]].round(3).to_string(index=False))
    for dim in [c for c in ("univ", "w", "k", "a", "vr", "gate", "side", "H", "s", "mode", "exit") if c in g and g[c].notna().any()]:
        t = v.groupby(dim)[["sh_is", "sh_oos"]].median().round(2)
        P(f"   median Sharpe by {dim}: " + "; ".join(f"{i}: IS {r.sh_is} OOS {r.sh_oos}" for i, r in t.iterrows()))
P("")
P("SELECTION (IS only):", json.dumps(sel))
for c, o in ev.items():
    P("=" * 80)
    P(c)
    m = o["mtm"]
    hk = m["kelly_half_is"]
    b1 = m["sh_oos"] >= 1.5 and m["ret25"] > 0 and m["ret26"] > 0
    st = o["stress_lat2_cost1.5"]["sh_oos"]
    b2 = o["lomo_min"] >= 1.0 and o["loco_min"] >= 1.0 and st >= 0.8
    P(f" 1x MTM: IS Sharpe {m['sh_is']:.2f}, OOS Sharpe {m['sh_oos']:.2f}, 2025 {m['ret25']:+.2%}, 2026 {m['ret26']:+.2%},"
      f" CAGR IS {m['cagr_is']:+.2%} OOS {m['cagr_oos']:+.2%}, half-Kelly(IS) {hk:.2f}")
    P(f" bar1 (OOS Sharpe>=1.5, both years >0): {'PASS' if b1 else 'FAIL'}")
    P(f" bar2: LOMO min {o['lomo_min']:.2f} ({o['lomo_argmin']}), LOCO min {o['loco_min']:.2f} ({o['loco_argmin']}),"
      f" costs x1.5 + 1 bar OOS Sharpe {st:.2f} -> {'PASS' if b2 else 'FAIL'}")
    for k in ("lat0_optimistic", "lat2", "cost1.5", "stress_lat2_cost1.5"):
        x = o[k]
        P(f"   {k}: IS Sh {x['sh_is']:.2f} OOS Sh {x['sh_oos']:.2f}, bp/trade IS {x['bp_is']:.1f} OOS {x['bp_oos']:.1f}, n {x['n_is']}/{x['n_oos']}")
    P(" per year:", json.dumps(o["per_year"]))
    P(" leverage table (OOS sim restarted 2025-01-01, 5k account, lots):")
    for x in o["leverage"]:
        P(f"   L={x['L']:>2}: OOS ret {x['oos_ret']:+.1%} MDD {x['oos_mdd']:.1%} liq {x['oos_liq']} {x['oos_liq_t']} | IS CAGR {x['is_cagr']:+.1%} IS MDD {x['is_mdd']:.1%} liq {x['is_liq']}")
    P(f" supportable L (MDD<=35%, no liq, <= half-Kelly {hk:.2f}): {o['supportable_L']}")
    P(" exec:", json.dumps(o["exec"]))
    P(" corr with current book (OOS daily):", round(o["corr_book_oos"], 3))
open(f"{W}/out/report.txt", "w").write("\n".join(lines))
print("\n".join(lines))
