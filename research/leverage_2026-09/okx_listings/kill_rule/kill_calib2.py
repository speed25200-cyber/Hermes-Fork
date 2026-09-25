import sys
exec(open('kill_calib.py').read().split("rows = []")[0])


def first_kill2(seq, N, thr, dd):
    c = np.cumsum(np.insert(seq, 0, 0.0))
    u = np.cumsum(np.insert(seq * 0.1, 0, 0.0))      # sleeve P&L in units of its cap (a tranche is ~10% of the cap)
    peak = 0.0
    for t in range(1, len(seq) + 1):
        peak = max(peak, u[t - 1])
        if dd is not None and u[t] - peak <= -dd:
            return t
        if N and t >= N and (c[t] - c[t - N]) / N <= thr:
            return t
    return None


rows = []
for N, thr, dd in ((25, 0.0, None), (60, -0.03, None), (60, -0.03, 0.25), (60, -0.03, 0.20), (50, -0.03, 0.25),
                   (60, -0.05, 0.25), (60, -0.02, 0.25), (0, 0, 0.25), (0, 0, 0.30), (0, 0, 0.20)):
    rec = dict(N=N, thr=thr, dd=dd)
    for name, ps in paths.items():
        ks = [first_kill2(p, N, thr, dd) for p in ps]
        rec[name] = round(float(np.mean([k is not None for k in ks])), 3)
        if name == 'edge_zero':
            done = [k for k in ks if k is not None]
            rec['zero_median_trades'] = int(np.median(done)) if done else None
    rows.append(rec)
print(pd.DataFrame(rows).to_string(index=False))
