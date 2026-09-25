"""Event table + hourly arrays for the OKX-listing events in the newlisting/sim.py format (local hours from t0).
events.parquet: sym (=instId), t0 (hour of the first trade, ms), spot_first (oldest known spot market: Binance spot,
OKX spot listTime, or t0-31d when an OKX spot archive file exists >= 31 days before), cls, plus age columns.
panel2.npz: o,h,l,c (OKX last price; hours without trades carry the previous close while the contract trades),
bo..bc (OKX BTC-USDT-SWAP), fund (OKX realized funding settled in (t, t+1h]), fund_okx (NaN), okx_on."""
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import *
H = 240
HOUR = 3600000
suffix = '_all' if '--all' in sys.argv else ''
out = os.path.join(D, 'sim' + suffix)
os.makedirs(out, exist_ok=True)
t0s = pd.read_csv(os.path.join(D, f'ev_t0{suffix}.csv'))
ages = pd.read_csv(os.path.join(D, f'ev_age{suffix}.csv'), parse_dates=['bn_spot', 'okx_spot_live', 'bybit_perp'])
ea = pd.read_csv(os.path.join(D, 'okx_events_a.csv'))
h1 = pd.read_parquet(os.path.join(D, f'ev_h1{suffix}.parquet'))
fu = pd.read_parquet(os.path.join(D, f'ev_funding{suffix}.parquet'))
btc = pd.read_parquet(os.path.join(D, 'btc_h1.parquet')).set_index('t')
ev = t0s.merge(ages, on='instId').merge(ea[['instId', 'cls', 'bn_first']], on='instId', how='left')
ev['t0_exact'] = ev.t0
ev['t0'] = (ev.t0 // HOUR) * HOUR
t0d = pd.to_datetime(ev.t0, unit='ms')
old_okx = np.where(ev.okx_spot_old, t0d - pd.Timedelta(days=31), pd.NaT)
ev['spot_first'] = pd.concat([ev.bn_spot, ev.okx_spot_live, pd.Series(pd.to_datetime(old_okx))], axis=1).min(axis=1)
ev['sym'] = ev.instId
ev = ev.sort_values('t0').reset_index(drop=True)
n = len(ev)
A = {k: np.full((n, H), np.nan) for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund_okx')}
A['fund'] = np.zeros((n, H))
A['okx_on'] = np.zeros((n, H), bool)
g = dict(tuple(h1.groupby('instId')))
gf = dict(tuple(fu.groupby('instId')))
for i, r in ev.iterrows():
    clock = r.t0 + np.arange(H, dtype=np.int64) * HOUR
    k = g[r.sym].drop_duplicates('t').set_index('t').reindex(clock)
    last = np.where(k.c.notna().values)[0]
    if len(last):
        lo, hi = last[0], last[-1]
        c = k.c.ffill()
        for a in 'ohl':
            k[a] = k[a].fillna(c)
        k.loc[k.index[:lo], ['o', 'h', 'l', 'c']] = np.nan
        k.loc[k.index[hi + 1:], ['o', 'h', 'l', 'c']] = np.nan
        k['c'] = np.where(np.arange(H) <= hi, c, np.nan)
        for a in 'ohlc':
            A[a][i] = k[a].values
        A['okx_on'][i, lo:hi + 1] = True
    b = btc.reindex(clock)
    for a in 'ohlc':
        A['b' + a][i] = b[a].values
    if r.sym in gf:
        ff = gf[r.sym]
        idx = np.ceil((ff.funding_time.values - r.t0) / HOUR).astype(np.int64) - 1
        ok = (idx >= 0) & (idx < H)
        np.add.at(A['fund'][i], idx[ok], ff.rate.values[ok])
A['fund_src_okx'] = np.ones(n, bool)
np.savez_compressed(os.path.join(out, 'panel2.npz'), **A)
ev.to_parquet(os.path.join(out, 'events.parquet'))
nt = ev.spot_first.isna() | ((t0d.loc[ev.index] - ev.spot_first).dt.days <= 30)
print(n, 'events; newtok', int(nt.sum()), '; by class', ev.cls.value_counts().to_dict(),
      '; bars/event median', int(np.median(np.isfinite(A['c']).sum(1))), '; <170 bars', int((np.isfinite(A['c']).sum(1) < 170).sum()))
