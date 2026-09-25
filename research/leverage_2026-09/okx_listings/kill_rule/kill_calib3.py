import sys
src = open('kill_calib.py').read().split("rows = []")[0]
src = src.replace("groups = lambda d: [g.net.values for _, g in d.sort_values('entry_t').groupby('i', sort=False)]",
                  "groups = lambda d: [np.c_[g.net.values, g.notional.values / g.E_before.values] for _, g in d.sort_values('entry_t').groupby('i', sort=False)]")
src = src.replace("H0 = [g - m for g in H1]", "H0 = [np.c_[g[:, 0] - m, g[:, 1]] for g in H1]").replace("Hh = [g - m / 2 for g in H1]", "Hh = [np.c_[g[:, 0] - m / 2, g[:, 1]] for g in H1]")
exec(src)
print('median tranche notional / equity', float(np.median(np.concatenate([g[:, 1] for g in H1]))))


def fk(seq, N, thr, loss, dd):
    net, w = seq[:, 0], seq[:, 1]
    c = np.cumsum(np.insert(net, 0, 0.0))
    u = np.cumsum(np.insert(net * w, 0, 0.0))
    peak = 0.0
    for t in range(1, len(net) + 1):
        peak = max(peak, u[t - 1])
        if dd is not None and u[t] - peak <= -dd:
            return t
        if t >= N:
            if (c[t] - c[t - N]) / N <= thr:
                return t
            if loss is not None and u[t] - u[t - N] <= -loss:
                return t
    return None


rows = []
for N, thr, loss, dd in ((25, 0.0, 0.10, None), (60, -0.03, None, None), (60, -0.03, 0.10, None), (60, -0.03, 0.15, None),
                         (60, -0.03, 0.20, None), (60, -0.03, None, 0.20), (60, -0.03, None, 0.25), (60, -0.03, None, 0.30),
                         (50, -0.03, None, 0.25), (60, -0.05, None, 0.25)):
    rec = dict(N=N, thr=thr, loss=loss, dd=dd)
    for name, ps in paths.items():
        ks = [fk(p, N, thr, loss, dd) for p in ps]
        rec[name] = round(float(np.mean([k is not None for k in ks])), 3)
        if name == 'edge_zero':
            done = [k for k in ks if k is not None]
            rec['zero_median'] = int(np.median(done)) if done else None
    rows.append(rec)
print(pd.DataFrame(rows).to_string(index=False))
