"""Add ZETA (Binance listed 28.5 h after OKX's first trade, excluded as bn_covered via the funding-stamp timing) to the
OKX-extra set and rerun."""
import sys, json
sys.path.insert(0, SP_ := "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import get, trades_url, read_trades, day8
from v_common import *
import types
HOUR = 3600000
inst = 'ZETA-USDT-SWAP'
t0x = int(pd.Timestamp('2024-02-01 03:28:34.813').value // 10 ** 6)
d0 = day8(t0x)
parts = []
for k in range(0, 11):
    b = get(trades_url(inst, d0 + pd.Timedelta(days=k)), tries=3)
    if b is not None:
        tr = read_trades(b).sort_values('t'); h = (tr.t // HOUR) * HOUR; g = tr.groupby(h).px
        parts.append(pd.DataFrame({'t': g.first().index.astype('int64'), 'o': g.first().values, 'h': g.max().values, 'l': g.min().values, 'c': g.last().values}))
df = pd.concat(parts).groupby('t').agg(o=('o', 'first'), h=('h', 'max'), l=('l', 'min'), c=('c', 'last'))
t0 = (t0x // HOUR) * HOUR
clock = t0 + np.arange(H, dtype=np.int64) * HOUR
k = df.reindex(clock); c = k.c.ffill(); last = np.where(k.c.notna())[0]; hi = last[-1]
for a in 'ohl': k[a] = k[a].fillna(c)
k['c'] = c; k.loc[k.index[hi + 1:], ['o', 'h', 'l', 'c']] = np.nan
btc = pd.read_parquet(SP + '/xlist/data/btc_h1.parquet').set_index('t').reindex(clock)
fa = pd.read_parquet(SP + '/xvenue/data/okx_funding_all.parquet'); fa = fa[fa.instId == inst].sort_values('funding_time')
iv = fa.funding_time.diff().fillna(8 * HOUR); ft = (fa.funding_time - iv.astype('int64')).values
fund = np.zeros(H); idx = np.ceil((ft - t0) / HOUR).astype(int) - 1; ok = (idx >= 0) & (idx < H); np.add.at(fund, idx[ok], fa.real_funding_rate.values[ok])
Do = load_okx()
Z = types.SimpleNamespace()
arr = dict(o=k.o.values, h=k.h.values, l=k.l.values, c=k.c.values, bo=btc.o.values, bh=btc.h.values, bl=btc.l.values, bc=btc.c.values, fund=fund)
on = np.zeros(H, bool); on[last[0]:hi + 1] = True
for key in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund'):
    setattr(Z, key, np.vstack([getattr(Do, key)[:, :H], arr[key][None, :]]))
Z.okx_on = np.vstack([Do.okx_on[:, :H], on[None, :]])
lr = np.diff(np.log(Z.c[-1:]), axis=1); lr[:, 0] = np.nan; m = np.isfinite(lr); x = np.where(m, lr, 0.0)
cs, cs2, cn = np.cumsum(x, 1), np.cumsum(x * x, 1), np.cumsum(m, 1)
vd = np.sqrt(np.maximum((cs2 - cs ** 2 / np.maximum(cn, 1)) / np.maximum(cn - 1, 1), 0) * 24)
Z.vol_d = np.vstack([Do.vol_d[:, :H - 1], vd]); Z.vol_n = np.vstack([Do.vol_n[:, :H - 1], cn])
Z.g0 = np.append(Do.g0, (t0 - sim.G0) // HOUR); Z.newtok = np.append(Do.newtok, True); Z.year = np.append(Do.year, 2024)
Z.ev = pd.concat([Do.ev[['sym', 't0']], pd.DataFrame(dict(sym=[inst], t0=[t0]))], ignore_index=True)
Z.n, Z.H, Z.sigma_ref = len(Z.ev), H, SIG
out = {}
for pn, a, b in [('2022-2026', '2022-01-01', '2026-09-25'), ('2022-2024', '2022-01-01', '2025-01-01')]:
    r0, t0_ = run(Do, a, b); r1, t1 = run(Z, a, b)
    zt = t1[t1.sym == inst]
    out[pn] = dict(sharpe_author=round(sharpe(r0['ret']), 3), sharpe_with_zeta=round(sharpe(r1['ret']), 3), zeta_trades=zt[['tranche', 'ret', 'reason']].round(3).to_dict('records'))
    print(pn, out[pn])
json.dump(out, open(SP + '/xlist/verify/v8_zeta.json', 'w'), indent=1)
