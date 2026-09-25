"""Step 5: signals on the 4-hour decision grid (decision at the close of bar k when (hour(k)+1) % 4 == 0).
Every quantity at row k uses closes/funding/volume up to and including bar k only. Forward returns (targets) are
stored separately and used only for IC signs and LightGBM labels."""
import numpy as np, pandas as pd

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"
SIGS = ["r1h", "r4h", "r1d", "m3d", "m7d", "m30d", "fund", "dfund", "vshock", "age", "rr1d", "rm7d"]


def ffill2(a):
    a = a.copy()
    T = a.shape[0]
    mask = np.isfinite(a)
    idx = np.where(mask, np.arange(T)[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    out = a[idx, np.arange(a.shape[1])[None, :]]
    first = mask.argmax(axis=0)
    out[np.arange(T)[:, None] < first[None, :]] = np.nan
    out[:, ~mask.any(axis=0)] = np.nan
    return out


def main():
    z = np.load(f"{W}/data/panel.npz")
    hrs = z["idx"]; syms = z["syms"]
    C = z["close"].astype(np.float64)
    T, N = C.shape
    Cf = ffill2(C)
    alive = np.isfinite(C)
    L = np.log(Cf)
    lr = np.zeros_like(L); lr[1:] = L[1:] - L[:-1]; lr = np.nan_to_num(lr, nan=0.0)
    valid = np.zeros((T, N), bool); valid[1:] = alive[1:] & alive[:-1]
    lr = np.where(valid, lr, 0.0)
    m = np.where(valid.sum(1) > 0, lr.sum(1) / np.maximum(valid.sum(1), 1), 0.0)  # EW market hourly log return
    Mcum = np.cumsum(m)
    # rolling beta (720h) via cumsums, only hours where the coin is valid
    Mi = np.where(valid, m[:, None], 0.0)

    def cs(a):
        out = np.zeros((T + 1,) + a.shape[1:]); np.cumsum(a, axis=0, out=out[1:]); return out
    cXY = cs(lr * Mi); cX = cs(lr); cY = cs(Mi); cYY = cs(Mi * Mi); cN = cs(valid.astype(np.float64))
    cX2 = cs(lr * lr)
    ret = np.where(valid, np.expm1(lr), 0.0)
    cR = cs(ret); cR2 = cs(ret * ret)
    F = np.nan_to_num(z["funding"].astype(np.float64)); cF = cs(F)
    QV = np.nan_to_num(z["qv"].astype(np.float64)); cQ = cs(QV)
    del Mi

    ts = pd.to_datetime(hrs * 3600, unit="s", utc=True)
    K = np.where((hrs + 1) % 4 == 0)[0]
    K = K[(K >= 24 * 31 + 720)]  # enough look-back
    nK = len(K)
    dec_time = ts[K] + pd.Timedelta(hours=1)  # decision instant (close of bar k)

    def win(c, k, n):  # sum over bars k-n+1..k
        return c[k + 1] - c[k + 1 - n]

    out = {}
    Kp = K
    Lk = L[Kp]
    out["r1h"] = -(Lk - L[Kp - 1]); out["r4h"] = -(Lk - L[Kp - 4]); out["r1d"] = -(Lk - L[Kp - 24])
    out["m3d"] = Lk - L[Kp - 72]; out["m7d"] = L[Kp - 24] - L[Kp - 168]; out["m30d"] = L[Kp - 24] - L[Kp - 720]
    f24 = win(cF, Kp, 24); f168 = win(cF, Kp, 168)
    out["fund"] = -f24; out["dfund"] = -(f24 - f168 / 7)
    q24 = win(cQ, Kp, 24); q720 = win(cQ, Kp, 720)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["vshock"] = np.where((q24 > 0) & (q720 > 0), np.log(q24 / (q720 / 30)), np.nan)
        n = win(cN, Kp, 720)
        sxy = win(cXY, Kp, 720); sx = win(cX, Kp, 720); sy = win(cY, Kp, 720); syy = win(cYY, Kp, 720)
        beta = (sxy - sx * sy / n) / (syy - sy * sy / n)
        beta = np.where(n >= 168, beta, np.nan)
        n7 = win(cN, Kp, 168)
        mu = win(cR, Kp, 168) / n7
        vol = np.sqrt(np.maximum(win(cR2, Kp, 168) / n7 - mu * mu, 0))
        vol = np.where(n7 >= 48, vol, np.nan)
    mk = Mcum[Kp][:, None]
    out["rr1d"] = -((Lk - L[Kp - 24]) - beta * (mk - Mcum[Kp - 24][:, None]))
    out["rm7d"] = (L[Kp - 24] - L[Kp - 168]) - beta * (Mcum[Kp - 24][:, None] - Mcum[Kp - 168][:, None])
    # age: days since first Binance trading day (daily volume file starts 2020-01-01)
    daily = pd.read_parquet("/home/user/data/daily_volume_24cdb49d3f.parquet")
    first = []
    for s in syms:
        d = daily[s].dropna(); d = d[d > 0]
        first.append(d.index[0])
    first = pd.DatetimeIndex(first)
    age_days = (dec_time.values[:, None] - first.values[None, :]) / np.timedelta64(1, "D")
    out["age"] = np.log(np.maximum(age_days, 0.5))
    # validity: coin alive at k; signals need the look-back closes to exist
    ok = alive[Kp]
    for s in SIGS:
        out[s] = np.where(ok & np.isfinite(out[s]), out[s], np.nan).astype(np.float32)
    # ranks and ADV (daily, PIT), aligned to decision day
    rk = pd.read_parquet(f"{W}/data/ranks.parquet").reindex(columns=list(syms))
    adv = daily.loc["2021-06-01":].reindex(columns=list(syms))
    lst = pd.read_parquet(f"{W}/data/okx_listed_all.parquet").reindex(columns=list(syms)).fillna(False).astype(bool)
    adv = adv.rolling(30, min_periods=7).mean().shift(1)
    day = dec_time.floor("1D")
    rank_k = rk.reindex(day).to_numpy(np.float32)
    adv_k = adv.reindex(day).to_numpy(np.float32)
    # forward returns (labels only)
    fw = {}
    for R in (4, 8, 24):
        kk = np.minimum(Kp + R, T - 1)
        f = L[kk] - Lk
        f[Kp + R > T - 1] = np.nan
        fw[f"fwd{R}"] = np.where(ok, f, np.nan).astype(np.float32)
    np.savez(f"{W}/data/features.npz", K=K, hrs=hrs[K], syms=syms, beta=beta.astype(np.float32), vol=vol.astype(np.float32),
             rank=rank_k, adv=adv_k, Ck=Cf[Kp].astype(np.float64), alive=ok, **out, **fw)
    print("features", nK, N, {s: float(np.isfinite(out[s]).mean().round(3)) for s in SIGS})
    # hourly arrays for the simulator
    H = z["high"].astype(np.float64); Lo = z["low"].astype(np.float64)
    H = np.where(np.isfinite(H) & alive, H, Cf); Lo = np.where(np.isfinite(Lo) & alive, Lo, Cf)
    np.savez(f"{W}/data/simarrays.npz", Cf=Cf, H=H, Lo=Lo, F=F, hrs=hrs, syms=syms, mkt=m)


if __name__ == "__main__":
    main()
