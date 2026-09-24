import numpy as np, pandas as pd
Z = np.load('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/panel.npz', allow_pickle=True)
coins = [str(c) for c in Z['coins']]; time = pd.to_datetime(Z['time'].astype('int64'), unit='ns', utc=True)
print(len(coins), coins); print(time[0], time[-1], len(time))
bc, oc, bmc, omc = Z['bc'], Z['oc'], Z['bmc'], Z['omc']
fb, fo = Z['fb'], Z['fo']
rows = []
yrs = time.year
for j, c in enumerate(coins):
    v = np.isfinite(bc[:, j]) & np.isfinite(oc[:, j])
    r = np.log(bc[v, j] / oc[v, j]) * 1e4
    rm = np.log(bmc[:, j] / omc[:, j]) * 1e4
    first = time[np.argmax(v)]
    d = dict(coin=c, first=str(first.date()), n=v.sum(), gap_b=int((~np.isfinite(bc[:, j]))[np.argmax(v):].sum()), gap_o=int((~np.isfinite(oc[:, j]))[np.argmax(v):].sum()),
             gap_om=int((~np.isfinite(omc[:, j]))[np.argmax(v):].sum()), gap_bm=int((~np.isfinite(bmc[:, j]))[np.argmax(v):].sum()),
             med_bp=np.nanmedian(r), p01=np.nanpercentile(r, 0.1), p999=np.nanpercentile(r, 99.9), mk_p999=np.nanpercentile(np.abs(rm[np.isfinite(rm)]), 99.9))
    # funding counts per year after listing
    for y in range(2022, 2027):
        m = (yrs == y) & v
        d[f'nb{y}'] = int((fb[m, j] != 0).sum()); d[f'no{y}'] = int((fo[m, j] != 0).sum())
    rows.append(d)
df = pd.DataFrame(rows); pd.set_option('display.width', 300); pd.set_option('display.max_columns', 40)
print(df.round(1).to_string())
