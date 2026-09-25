"""Verifier step 2: adversarial checks on the TAIL pre-registered pick (and TOP30 comparison pick).
 a) survivorship: delisted coins present in the panel / tail universe;
 b) bad prints: worst coin-hour P&L contributions vs Binance mark-price range; global close-outside-mark-range rate;
 c) independent vectorised daily P&L (gross, funding, costs) vs the numba simulator;
 d) dependence on events/coins: leg decomposition, worst days, winsorised coin returns (sensitivity only);
 e) intrabar-drawdown assumption: hourly-close MDD vs 'all names at worst at once'."""
import sys, json
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail")
import numpy as np, pandas as pd
from lt_core import *
import s8_eval as S8

V = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail_verify"
D = S8.D
rep = {}
mk = np.load(f"{W}/data/mark_hl.npz")
MH, ML = mk["MH"], mk["ML"]
hrs_ts = pd.to_datetime(D.hrs * 3600, unit="s", utc=True)

# a) survivorship
daily = pd.read_parquet("/home/user/data/daily_volume_24cdb49d3f.parquet")
lastd = {s: daily[s][daily[s] > 0].index[-1] for s in D.syms}
dead = [s for s in D.syms if lastd[s] < pd.Timestamp("2026-08-01", tz="UTC")]
rk = D.f["rank"]
tail_ever = set(np.array(D.syms)[((rk >= 31) & (rk <= 150) & D.f["alive"]).any(0)])
rep["survivorship"] = dict(panel_syms=len(D.syms), dead_before_2026_08=len(dead), dead_in_tail_universe=len(set(dead) & tail_ever),
                           examples=sorted(set(dead) & tail_ever)[:25])
print(rep["survivorship"], flush=True)

# b) global bad-print rate: alive hours where last close lies outside the mark [low, high] by > 20%
C = D.Cf
alive_h = np.isfinite(MH) & (D.H != D.Lo)
with np.errstate(invalid="ignore"):
    bad = alive_h & ((C > MH * 1.2) | (C < ML / 1.2))
rep["bad_close_vs_mark_frac"] = float(bad.sum() / max(alive_h.sum(), 1))
rep["bad_close_vs_mark_n"] = int(bad.sum())
oos_h = (hrs_ts >= OOS0) & (hrs_ts < OOS1)
rep["bad_close_vs_mark_n_oos"] = int(bad[oos_h].sum())
bi = np.argwhere(bad[oos_h])
rep["bad_examples_oos"] = [(str(hrs_ts[oos_h][i]), D.syms[j]) for i, j in bi[:15]]
print("bad prints", rep["bad_close_vs_mark_n"], rep["bad_close_vs_mark_n_oos"], rep["bad_examples_oos"][:10], flush=True)


def book_arrays(cfg):
    rows_all, Wt, sc, e = S8.weights_for(cfg)
    ro, wo = S8.sub(rows_all, Wt, OOS0, OOS1)
    return ro, wo


def vectorised(ro, wo, R, floor=3.0, cap=None, E0=5000.0):
    K = D.K[ro]
    nxt = np.minimum(K + R, C.shape[0] - 1)
    ret = C[nxt] / C[K] - 1
    if cap is not None:
        ret = np.clip(ret, -cap, cap)
    cF = np.vstack([np.zeros((1, C.shape[1])), np.cumsum(D.F, axis=0)])
    fsum = cF[nxt + 1] - cF[K + 1]
    gross_c = wo * np.nan_to_num(ret)
    gross = gross_c.sum(1)
    fund = (wo * fsum).sum(1)
    dw = np.abs(np.diff(np.vstack([np.zeros((1, wo.shape[1])), wo]), axis=0))
    adv = D.f["adv"][ro].astype(np.float64)
    adv = np.where(np.isfinite(adv) & (adv > 0), adv, 1e6)
    usd = np.maximum(dw * E0, 1.0)
    slip = np.maximum(floor, np.exp(COST_A + COST_B * np.log(adv) + COST_C * np.log(usd)))
    cost = (dw * (FEE_BP + slip) * 1e-4).sum(1)
    long_c = np.where(wo > 0, gross_c, 0).sum(1)
    short_c = np.where(wo < 0, gross_c, 0).sum(1)
    return pd.DataFrame(dict(gross=gross, fund=fund, cost=cost, net=gross - fund - cost, long=long_c, short=short_c),
                        index=D.dec_time[ro])


cfgT = dict(univ="TAIL", R=24, signal="m30d", neutral="dollar", hyst=1, kind="single")
roT, woT = book_arrays(cfgT)
vt = vectorised(roT, woT, 24)
res = S8.SIM(cfgT, roT, woT, OOS0, OOS1, univ="TAIL")
rsim = daily_returns(res["eq"])
rep["vectorised_TAIL"] = dict(sharpe_net=sharpe(vt.net), sharpe_gross=sharpe(vt.gross), ann_gross=float(vt.gross.mean() * 365),
                              ann_fund=float(vt.fund.mean() * 365), ann_cost=float(vt.cost.mean() * 365),
                              ann_long_leg=float(vt.long.mean() * 365), ann_short_leg=float(vt.short.mean() * 365),
                              corr_with_sim=float(np.corrcoef(vt.net.values[:len(rsim)], rsim.values[:len(vt)])[0, 1]) if len(rsim) else None,
                              sim_sharpe=sharpe(rsim))
print("vectorised", rep["vectorised_TAIL"], flush=True)

# d) event dependence: worst days, drop worst k days, winsorised coin 24h returns
s = rsim.sort_values()
rep["worst_days_TAIL"] = {str(k.date()): round(float(v), 4) for k, v in s.head(8).items()}
rep["best_days_TAIL"] = {str(k.date()): round(float(v), 4) for k, v in s.tail(5).items()}
for k in (5, 10, 20):
    rep[f"sharpe_drop_worst_{k}_days"] = sharpe(s.iloc[k:])
    rep[f"sharpe_drop_best_{k}_days"] = sharpe(s.iloc[:-k])
for cap in (0.5, 0.3, 0.2):
    v = vectorised(roT, woT, 24, cap=cap)
    rep[f"sharpe_net_capped_coin_ret_{cap}"] = sharpe(v.net)
    rep[f"sharpe_gross_capped_coin_ret_{cap}"] = sharpe(v.gross)
print({k: v for k, v in rep.items() if k.startswith("sharpe_") or k.startswith("worst")}, flush=True)

# b2) worst coin-hour contributions in the OOS TAIL book (approx: position = weight x 5000 at decision close)
K = D.K[roT]
contrib = []
for i, k in enumerate(K):
    j = np.where(woT[i] != 0)[0]
    kk = min(k + 24, C.shape[0] - 1)
    q = woT[i, j] * 5000 / C[k, j]
    hr = (C[k + 1:kk + 1, j] - C[k:kk, j]) * q
    a, b = np.unravel_index(np.argsort(hr, axis=None)[:3], hr.shape)
    for aa, bb in zip(a, b):
        h = k + 1 + aa
        contrib.append((float(hr[aa, bb]), str(hrs_ts[h]), D.syms[j[bb]], float(woT[i, j[bb]]), float(C[h - 1, j[bb]]), float(C[h, j[bb]]),
                        float(MH[h, j[bb]]), float(ML[h, j[bb]])))
cdf = pd.DataFrame(contrib, columns=["pnl", "hour", "sym", "w", "c_prev", "c", "mark_hi", "mark_lo"]).sort_values("pnl").head(20)
cdf["close_in_mark_range"] = (cdf.c <= cdf.mark_hi * 1.05) & (cdf.c >= cdf.mark_lo / 1.05)
rep["worst_coin_hours_TAIL"] = cdf.round(5).to_dict("records")
print(cdf.round(4).to_string(), flush=True)

# e) intrabar assumption: hourly-close MDD vs worst-case intrabar, leverage 1..3
for L in (1, 2, 3):
    m = metrics(S8.SIM(cfgT, roT, woT, OOS0, OOS1, univ="TAIL", lev=L))
    rep[f"TAIL_L{L}_mdd_hourly_close"] = m["mdd"]; rep[f"TAIL_L{L}_mdd_intrabar_worstcase"] = m["mdd_ib"]
print({k: v for k, v in rep.items() if k.startswith("TAIL_L")}, flush=True)
json.dump(rep, open(f"{V}/v2_adversarial.json", "w"), indent=1, default=str)
