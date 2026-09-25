"""Copies of the OKX-extra sim panel: (a) excl = MET, RE removed; (b) reanchor = MET, RE rebuilt from TGE hour
(same construction as xlist/panel.py: OKX last-price 1H, prev-close fill, OKX BTC-USDT-SWAP, realized funding)."""
import os, numpy as np, pandas as pd
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
SRC = SP + '/xlist/data/sim'; HERE = SP + '/xlist/verify/premkt'
H, HOUR = 240, 3600000
ev = pd.read_parquet(SRC + '/events.parquet'); P = dict(np.load(SRC + '/panel2.npz'))
btc = pd.read_parquet(SP + '/xlist/data/btc_h1.parquet').set_index('t')
fu = pd.read_parquet(SP + '/xlist/data/ev_funding.parquet')
bars = pd.read_parquet(HERE + '/okx_pm_h1.parquet')
TGE = {'MET-USDT-SWAP': '2025-10-23 14:00', 'RE-USDT-SWAP': '2026-06-18 14:00'}


def save(name, ev2, P2):
    d = f'{HERE}/{name}'; os.makedirs(d, exist_ok=True)
    np.savez_compressed(d + '/panel2.npz', **P2); ev2.to_parquet(d + '/events.parquet')


# (a) exclusion
keep = ~ev.sym.isin(list(TGE)).values
save('sim_okx_excl', ev[keep].reset_index(drop=True), {k: (v[keep] if v.shape[0] == len(ev) else v) for k, v in P.items()})
# (b) re-anchor
ev2 = ev.copy(); P2 = {k: v.copy() for k, v in P.items()}
for sym, tge in TGE.items():
    i = int(np.where(ev.sym.values == sym)[0][0])
    t0 = pd.Timestamp(tge).value // 10**6
    clock = t0 + np.arange(H, dtype=np.int64) * HOUR
    k = bars[bars.instId == sym].drop_duplicates('t').set_index('t').reindex(clock)
    have = np.where(k.c.notna().values)[0]
    lo, hi = have[0], have[-1]
    c = k.c.ffill()
    for a in 'ohl':
        k[a] = k[a].fillna(c)
    for a in 'ohlc':
        v = (c if a == 'c' else k[a]).values.astype(float).copy()
        v[:lo] = np.nan; v[hi + 1:] = np.nan
        P2[a][i] = v
    P2['okx_on'][i] = False; P2['okx_on'][i, lo:hi + 1] = True
    b = btc.reindex(clock)
    for a in 'ohlc':
        P2['b' + a][i] = b[a].values
    P2['fund'][i] = 0.0
    ff = fu[fu.instId == sym]
    idx = np.ceil((ff.funding_time.values - t0) / HOUR).astype(np.int64) - 1
    ok = (idx >= 0) & (idx < H)
    np.add.at(P2['fund'][i], idx[ok], ff.rate.values[ok])
    print(sym, 'bars', hi - lo + 1, 'funding pts in window', int(ok.sum()), 'old t0', pd.to_datetime(ev.t0[i], unit='ms'), '-> new', tge)
    ev2.loc[i, 't0'] = t0
save('sim_okx_reanchor', ev2, P2)
