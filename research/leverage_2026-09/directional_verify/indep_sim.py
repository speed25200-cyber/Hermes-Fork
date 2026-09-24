"""
Independent re-implementation (verifier) of the two 'Pick A' configs, written from scratch:
 - reads the RAW Binance zips (klines, markPriceKlines, fundingRate), not the researcher's parquet
 - builds TF bars with pandas resample (left-closed, left-labelled, UTC), signals known at bar END
 - fills at the open of the first 5m bar whose open_time == TF bar end (no same-bar fill)
 - plain-python event loop; cross margin, whole account behind one position
Also supports: extra fill delay (latency / look-ahead probe), alternative intrabar orderings,
harsher/cheaper cost sets and a liquidation that leaves the remaining maintenance equity (OKX-like
'partial' outcome) vs total loss.
"""
import zipfile, io, glob, sys, json
import numpy as np, pandas as pd

D = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional/data'
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional_verify/out'
IS0, IS1, OOS0, OOS1 = '2022-01-01', '2025-01-01', '2025-01-01', '2026-09-01'


def rz(p, names):
    with zipfile.ZipFile(p) as z:
        raw = z.read(z.namelist()[0]).decode()
    hdr = 0 if not raw[0].isdigit() else None
    df = pd.read_csv(io.StringIO(raw), header=hdr)
    df = df.iloc[:, :len(names)]
    df.columns = names
    return df


_cache = {}
def load_raw(sym):
    if sym in _cache: return _cache[sym]
    kn = ['t', 'o', 'h', 'l', 'c']
    k = pd.concat([rz(p, kn) for p in sorted(glob.glob(f'{D}/*_monthly_klines_{sym}_5m_*.zip'))]).drop_duplicates('t').set_index('t').sort_index()
    m = pd.concat([rz(p, kn) for p in sorted(glob.glob(f'{D}/*_markPriceKlines_{sym}_5m_*.zip'))]).drop_duplicates('t').set_index('t').sort_index()
    m.columns = ['mo', 'mh', 'ml', 'mc']
    f = pd.concat([rz(p, ['ct', 'iv', 'r']) for p in sorted(glob.glob(f'{D}/*_fundingRate_{sym}_*.zip'))])
    df = k.join(m, how='left')
    miss = df.mo.isna().sum()
    for a, b in [('mo', 'o'), ('mh', 'h'), ('ml', 'l'), ('mc', 'c')]:
        df[a] = df[a].fillna(df[b])
    df.index = pd.to_datetime(df.index, unit='ms', utc=True)
    fb = pd.to_datetime((f.ct.values // 300000) * 300000, unit='ms', utc=True)
    fs = pd.Series(f.r.values, index=fb)
    fs = fs[~fs.index.duplicated()]
    df['fund'] = fs.reindex(df.index)
    _cache[sym] = (df, miss)
    return df, miss


def tf_bars(df, rule):
    b = df[['o', 'h', 'l', 'c']].resample(rule, label='left', closed='left').agg({'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last'})
    return b.dropna()


def make_signals(df, rule, fam, p):
    """Return dict of 5m-indexed boolean arrays evL, evS, exL, exS and stop fraction (ATR) array,
    placed at the 5m bar whose OPEN time equals the TF bar END time."""
    b = tf_bars(df, rule)
    # drop the last TF bar if it is incomplete (resample would include a partial bar)
    step = pd.Timedelta(rule)
    c = b.c
    if fam == 'ema':
        f, s = p
        diff = c.ewm(span=f, adjust=False).mean() - c.ewm(span=s, adjust=False).mean()
        up = diff > 0; dn = diff < 0
        evL = up & ~up.shift(1, fill_value=False); evS = dn & ~dn.shift(1, fill_value=False)
        exL, exS = dn, up
    elif fam == 'donch':
        N = p; N2 = N // 2
        hiN = b.h.shift(1).rolling(N).max(); loN = b.l.shift(1).rolling(N).min()
        hi2 = b.h.shift(1).rolling(N2).max(); lo2 = b.l.shift(1).rolling(N2).min()
        evL = c > hiN; evS = c < loN; exL = c < lo2; exS = c > hi2
    prevc = c.shift(1)
    tr = np.maximum(b.h - b.l, np.maximum((b.h - prevc).abs(), (b.l - prevc).abs()))
    tr.iloc[0] = b.h.iloc[0] - b.l.iloc[0]
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean() / c
    known_at = b.index + step           # information is known when the TF bar closes
    out = {}
    for name, s in [('evL', evL), ('evS', evS), ('exL', exL), ('exS', exS)]:
        z = pd.Series(s.fillna(False).astype(bool).values, index=known_at).reindex(df.index).fillna(False)
        out[name] = z.values.astype(bool)
    out['atr'] = pd.Series(atr.values, index=known_at).reindex(df.index).ffill().values
    return out


def vol_multiplier(df):
    h1 = df.c.resample('1h', label='left', closed='left').last()
    v = np.log(h1).diff().rolling(168).std()
    vg = pd.Series(v.values, index=h1.index + pd.Timedelta('1h')).reindex(df.index).ffill()
    msk = (df.index >= IS0) & (df.index < IS1)
    ref = np.nanmedian(vg.values[msk])
    vm = np.clip(ref / vg.values, 0.25, 1.0)
    return np.nan_to_num(vm, nan=1.0), ref


def simulate(df, sig, win, L, stop_kind='fix', stop_fix=0.015, tp_R=2.0, volscale=None, delay=0,
             taker=0.0005, maker=0.0002, slip=0.0001, stop_slip=0.0002, stop_impact=0.10, mmr=0.004,
             cap=0.75, ordering='cons', liq_leaves_mm=False, record=False):
    a0, a1 = (IS0, IS1) if win == 'IS' else (OOS0, OOS1)
    idx = df.index
    i0 = idx.searchsorted(pd.Timestamp(a0, tz='UTC')); i1 = idx.searchsorted(pd.Timestamp(a1, tz='UTC'))
    o, h, l, c = (df[k].values for k in 'ohlc')
    mo, mh, ml = df.mo.values, df.mh.values, df.ml.values
    fund = df.fund.values
    evL, evS, exL, exS = (np.roll(sig[k], delay) if delay else sig[k] for k in ['evL', 'evS', 'exL', 'exS'])
    atr = sig['atr']
    atr = np.roll(atr, delay) if delay else atr
    E = 1.0; pos = 0; peak = 1.0; mdd = 0.0
    days = idx[i0:i1].normalize()
    day_eq = {}
    trades = []; nliq = 0; nstop = 0; ntp = 0
    k_liq = mmr + taker

    def liqpx(Q, P0, Ea, d):
        return (Q * P0 - Ea) / (Q * (1 - k_liq)) if d == 1 else (Ea + Q * P0) / (Q * (1 + k_liq))

    def close(px, fee):
        return Ea + pos * Q * (px - P0) - fee * Q * px

    dead = False
    for i in range(i0, i1):
        if dead:
            day_eq[days[i - i0]] = E; continue
        exit_px = None; liq = False
        if pos != 0:
            if not np.isnan(fund[i]):
                Ea -= pos * fund[i] * Q * mo[i]
            lp = liqpx(Q, P0, Ea, pos)
            # opening gap
            if (pos == 1 and mo[i] <= lp) or (pos == -1 and mo[i] >= lp):
                liq = True
            elif (pos == 1 and o[i] <= stop) or (pos == -1 and o[i] >= stop):
                exit_px, fee = o[i] * (1 - pos * stop_slip), taker; nstop += 1
            elif (pos == 1 and o[i] >= tp) or (pos == -1 and o[i] <= tp):
                exit_px, fee = tp, maker; ntp += 1
            elif (pos == 1 and (exL[i] or evS[i])) or (pos == -1 and (exS[i] or evL[i])):
                exit_px, fee = o[i] * (1 - pos * slip), taker
        if liq:
            nliq += 1
            E = (Q * mo[i] * mmr) if liq_leaves_mm else 0.0   # optional: keep maintenance margin
            trades.append(E / Epre - 1); pos = 0
            if E <= 1e-9: dead = True; mdd = 1.0
            day_eq[days[i - i0]] = E
            continue
        if exit_px is not None:
            E = max(close(exit_px, fee), 0.0); trades.append(E / Epre - 1); pos = 0
            if E <= 1e-9:
                dead = True; mdd = 1.0; day_eq[days[i - i0]] = E; continue
        if pos == 0 and (evL[i] or evS[i]):
            d = 1 if evL[i] else -1
            lev = L * (volscale[i] if volscale is not None else 1.0)
            P0 = o[i] * (1 + d * slip); N = lev * E; Q = N / P0; Epre = E; Ea = E - taker * N; pos = d
            lp = liqpx(Q, P0, Ea, d)
            liqd = (P0 - lp) / P0 if d == 1 else (lp - P0) / P0
            if d == 1 and lp <= 0: liqd = 1.0
            sd = stop_fix if stop_kind == 'fix' else (2 * atr[i] if not np.isnan(atr[i]) else 0.015)
            sd = min(sd, cap * liqd)
            stop = P0 * (1 - d * sd)
            tp = P0 * (1 + d * tp_R * sd) if tp_R > 0 else (np.inf if d == 1 else -np.inf)
        trough = None
        if pos != 0:
            lp = liqpx(Q, P0, Ea, pos)
            if pos == 1:
                lh, sh, th = ml[i] <= lp, l[i] <= stop, h[i] > tp
                pen = (stop - l[i]) / stop
            else:
                lh, sh, th = mh[i] >= lp, h[i] >= stop, l[i] < tp
                pen = (h[i] - stop) / stop
            if lh and (ordering == 'cons' or not sh):
                nliq += 1
                E = (Q * lp * mmr) if liq_leaves_mm else 0.0
                trades.append(E / Epre - 1); pos = 0
                if E <= 1e-9: dead = True; mdd = 1.0
                day_eq[days[i - i0]] = E
                continue
            ex = None
            if sh:
                ex, fee = stop * (1 - pos * (stop_slip + stop_impact * pen)), taker; nstop += 1
            elif th:
                ex, fee = tp, maker; ntp += 1
            if ex is not None:
                E = max(close(ex, fee), 0.0); trades.append(E / Epre - 1); pos = 0; trough = E
                if E <= 1e-9:
                    dead = True; mdd = 1.0; day_eq[days[i - i0]] = E; continue
            else:
                trough = Ea + pos * Q * ((l[i] if pos == 1 else h[i]) - P0)
        Ec = Ea + pos * Q * (c[i] - P0) if pos != 0 else E
        if trough is None: trough = Ec
        mdd = max(mdd, 1 - trough / peak, 1 - Ec / peak)
        peak = max(peak, Ec)
        day_eq[days[i - i0]] = Ec
    if pos != 0 and not dead:
        px = c[i1 - 1] * (1 - pos * slip)
        E = max(Ea + pos * Q * (px - P0) - taker * Q * px, 0.0); trades.append(E / Epre - 1)
        day_eq[days[-1]] = E
    s = pd.Series(day_eq)
    eq = np.concatenate([[1.0], s.values])
    r = pd.Series(eq[1:] / np.where(eq[:-1] > 0, eq[:-1], np.nan) - 1, index=s.index).fillna(0.0)
    nd = len(s); fin = s.iloc[-1]
    cagr = fin ** (365 / nd) - 1 if fin > 0 else -1.0
    sharpe = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else 0.0
    yrs = {}; prev = 1.0
    for y in sorted(set(s.index.year)):
        last = s[s.index.year == y].iloc[-1]; yrs[int(y)] = (last / prev - 1) if prev > 0 else -1.0; prev = last
    res = dict(win=win, L=L, final=float(fin), cagr=float(cagr), maxdd=float(mdd), worst_day=float(r.min()),
               sharpe=float(sharpe), trades=len(trades), liq=nliq, stops=nstop, tps=ntp, years=yrs)
    if record: res['trade_rets'] = trades; res['daily'] = s
    return res


if __name__ == '__main__':
    rows = []
    specs = [('ETHUSDT', '4h', 'ema', (20, 100), 'fix', 2.0, False),
             ('BTCUSDT', '4h', 'donch', 20, 'atr', 2.0, True)]
    for sym, rule, fam, p, sk, tpR, vs in specs:
        df, miss = load_raw(sym)
        print(sym, 'bars', len(df), 'mark bars missing (filled with last)', miss, 'funding events', df.fund.notna().sum())
        sig = make_signals(df, rule, fam, p)
        vm, ref = vol_multiplier(df) if vs else (None, None)
        for win in ['IS', 'OOS']:
            for L in [1, 3, 5, 10, 15, 20]:
                r = simulate(df, sig, win, L, stop_kind=sk, tp_R=tpR, volscale=vm)
                r.update(sym=sym, cfg=f'{rule}|{fam}|{p}|{sk}|tp{tpR}|{"vol" if vs else "fixed"}')
                rows.append(r)
                print(sym, win, L, 'final %.4g cagr %.4f maxdd %.4f worst %.4f sharpe %.3f trades %d liq %d' %
                      (r['final'], r['cagr'], r['maxdd'], r['worst_day'], r['sharpe'], r['trades'], r['liq']),
                      {k: round(v, 4) for k, v in r['years'].items()}, flush=True)
    pd.DataFrame(rows).to_csv(f'{OUT}/indep_pickA.csv', index=False)
