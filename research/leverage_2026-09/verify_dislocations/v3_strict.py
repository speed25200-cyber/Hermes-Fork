"""Strict-fill variant of gen_BC:
 - exit fills at the first minute >= decision+lat where the needed legs traded (walk FORWARD, never back);
 - single-leg modes (perp_only / okx_only) no longer require the hedge leg to trade in the fill minutes;
 - flags how many researcher trades had their exit walked back to a minute < decision+lat.
Runs the three picks and the whole B/C grid under strict fills."""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade")
from numba import njit
import core, s2_grid, s3_eval
NA32 = -2**31; NA16 = -32768

@njit(cache=False)
def gen_BC_strict(coin, t, widx, dev, med, cg, gdn, gup, rank, Wt, Wpc, Wph, Wpl, Whc, Whh, Whl, Wok,
                  rlo, rhi, k, gate, both, half, Hmax, lat, single):
    n = coin.shape[0]
    oc = np.empty(n, np.int32); ot = np.empty(n, np.int32); oe = np.empty(n, np.int32); ox = np.empty(n, np.int32)
    od = np.empty(n, np.int8); ope = np.empty(n); opx = np.empty(n); ohe = np.empty(n); ohx = np.empty(n)
    ore = np.empty(n); orx = np.empty(n); ohre = np.empty(n); ohrx = np.empty(n)
    oty = np.empty(n, np.int8); opr = np.empty(n); orow = np.empty(n, np.int64); owb = np.zeros(n, np.int8)
    cnt = 0; cur = -1; busy = -1
    NW = Wt.shape[0]
    for i in range(n):
        c = coin[i]
        if c != cur:
            cur = c; busy = -1
        if t[i] <= busy:
            continue
        if rank[i] < rlo or rank[i] > rhi:
            continue
        dv = dev[i]
        if not np.isfinite(dv) or not np.isfinite(med[i]):
            continue
        d = 0
        if dv <= -k:
            d = 1
        elif both and dv >= k:
            d = -1
        if d == 0:
            continue
        if gate == 1 and not cg[i]:
            continue
        if gate == 2 and not (gdn[i] or gup[i]):
            continue
        e = widx[i] + lat
        if e >= NW or Wt[e] != t[i] + lat or Wpc[e] == NA32:
            continue
        if (not single) and (Whc[e] == NA32 or not Wok[e]):
            continue
        lpe = Wpc[e] * 1e-5
        lhe = lpe + Whc[e] * 1e-5 if Whc[e] != NA32 else np.nan
        xm = -1; typ = 0
        for h in range(0, Hmax + 1):
            m = e + h
            if m >= NW or Wt[m] != t[i] + lat + h:
                break
            if h == Hmax:
                xm = m; typ = 0
                break
            if Wpc[m] == NA32 or Whc[m] == NA32 or not Wok[m]:
                continue
            devm = -Whc[m] * 1e-5 - med[i]
            thr = -0.5 * k if half else 0.0
            if d * devm >= thr:
                xm = m + lat; typ = 1
                break
        if xm < 0:
            continue
        # walk FORWARD to the first minute with the needed legs valid (max 20 minutes)
        ok = False
        for q in range(21):
            mm = xm + q
            if mm >= NW or Wt[mm] != Wt[e] + (mm - e):
                break
            if Wpc[mm] != NA32 and (single or (Whc[mm] != NA32 and Wok[mm])):
                xm = mm; ok = True
                if q > 0:
                    owb[cnt] = 1
                break
        if not ok:
            continue
        lpx = Wpc[xm] * 1e-5
        lhx = lpx + Whc[xm] * 1e-5 if Whc[xm] != NA32 else np.nan
        oc[cnt] = c; ot[cnt] = t[i]; oe[cnt] = Wt[e]; ox[cnt] = Wt[xm]; od[cnt] = d
        ope[cnt] = lpe; opx[cnt] = lpx; ohe[cnt] = lhe; ohx[cnt] = lhx
        ore[cnt] = (Wph[e] - Wpl[e]) if Wph[e] != NA16 and Wpl[e] != NA16 else 0.0
        orx[cnt] = (Wph[xm] - Wpl[xm]) if Wph[xm] != NA16 and Wpl[xm] != NA16 else 0.0
        ohre[cnt] = (Whh[e] - Whl[e]) if Whh[e] != NA16 and Whl[e] != NA16 else 0.0
        ohrx[cnt] = (Whh[xm] - Whl[xm]) if Whh[xm] != NA16 and Whl[xm] != NA16 else 0.0
        oty[cnt] = typ; opr[cnt] = abs(dv); orow[cnt] = i
        busy = Wt[xm]
        cnt += 1
    return (oc[:cnt], ot[:cnt], oe[:cnt], ox[:cnt], od[:cnt], ope[:cnt], opx[:cnt], ohe[:cnt], ohx[:cnt],
            ore[:cnt], orx[:cnt], ohre[:cnt], ohrx[:cnt], oty[:cnt], opr[:cnt], orow[:cnt])

_single = {"v": False}
def patched(*a):
    return gen_BC_strict(*a, _single["v"])

def run(D, fam, cfg, lat=1, cost_mult=1.0, strict=True):
    orig = core.gen_BC
    if strict:
        _single["v"] = cfg["mode"] in ("perp_only", "okx_only")
        core.gen_BC = patched
    try:
        tr = core.portfolio(core.gen(D, fam, cfg, lat=lat, cost_mult=cost_mult))
    finally:
        core.gen_BC = orig
    return tr

if __name__ == "__main__":
    D = core.Data()
    sel = json.load(open(f"{core.W}/out/selection.json"))
    res = {}
    # 1) how often did the original code walk back? compare exit minute to decision+lat
    for fam in ("B", "C"):
        c = sel["per_family"][fam]; f, cfg = s3_eval.parse(c)
        a = core.portfolio(core.gen(D, f, cfg, lat=1)); a = a[a.acc]
        b = run(D, f, cfg); b = b[b.acc]
        sa, sb = core.stats(core.portfolio(core.gen(D, f, cfg, lat=1))), core.stats(run(D, f, cfg))
        m = a.merge(b[["coin", "t", "tx", "net"]], on=["coin", "t"], how="outer", suffixes=("", "_s"), indicator=True)
        diffx = m[(m._merge == "both") & (m.tx != m.tx_s)]
        print(fam, c)
        print("  orig: sh_is %.3f sh_oos %.3f n_oos %d bp_oos %.1f r25 %.3f r26 %.3f" % (sa["sh_is"], sa["sh_oos"], sa["n_oos"], sa["bp_oos"], sa["ret25"], sa["ret26"]))
        print("  strict: sh_is %.3f sh_oos %.3f n_oos %d bp_oos %.1f r25 %.3f r26 %.3f" % (sb["sh_is"], sb["sh_oos"], sb["n_oos"], sb["bp_oos"], sb["ret25"], sb["ret26"]))
        print("  trades with different exit minute:", len(diffx), "of", int((m._merge == 'both').sum()), " only-orig:", int((m._merge=='left_only').sum()), " only-strict:", int((m._merge=='right_only').sum()))
        if len(diffx):
            print("  mean net diff on those (bp): %.1f" % (1e4 * (diffx.net - diffx.net_s).mean()))
        res[fam] = dict(orig=sa, strict=sb, n_exit_changed=int(len(diffx)))
        # stress under strict
        st = core.stats(run(D, f, cfg, lat=2, cost_mult=1.5))
        print("  strict stress lat2 cost1.5: sh_oos %.3f" % st["sh_oos"])
        res[fam]["strict_stress"] = st
    # 2) whole B/C grid under strict fills
    rows = []
    for fam, cfg in s2_grid.configs():
        if fam == "A":
            continue
        st = core.stats(run(D, fam, cfg))
        st.update(fam=fam, cid=s2_grid.cid(fam, cfg), **cfg); rows.append(st)
    S = pd.DataFrame(rows); S.to_csv("v3_grid_BC_strict.csv", index=False)
    G = pd.read_csv(f"{core.W}/out/grid_results.csv").set_index("cid")
    S = S.set_index("cid")
    for fam in ("B", "C"):
        s = S[S.fam == fam]; g = G.loc[s.index]
        print(fam, "grid IS q10/50/90 orig", np.round(np.quantile(g.sh_is, [.1, .5, .9]), 2), "strict", np.round(np.quantile(s.sh_is, [.1, .5, .9]), 2))
        print(fam, "grid OOS q10/50/90 orig", np.round(np.quantile(g.sh_oos, [.1, .5, .9]), 2), "strict", np.round(np.quantile(s.sh_oos, [.1, .5, .9]), 2))
        e = s[s.n_is >= 30].sort_values("sh_is", ascending=False).iloc[0]
        print(fam, "strict IS pick:", e.name, "sh_is %.3f sh_oos %.3f r25 %.3f r26 %.3f n_oos %d" % (e.sh_is, e.sh_oos, e.ret25, e.ret26, e.n_oos))
        res[fam + "_strict_pick"] = dict(cid=e.name, sh_is=float(e.sh_is), sh_oos=float(e.sh_oos), ret25=float(e.ret25), ret26=float(e.ret26))
    json.dump(res, open("v3_strict.json", "w"), indent=1, default=float)
