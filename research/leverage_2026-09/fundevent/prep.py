"""Decision-time features for every settlement event with 1m windows:
beta vs BTC (OLS on 1h log returns, bars that closed >= 1h before t: 168h window, >= 72 obs, clipped [0.2, 3]),
24h quote volume (bars closed >= 1h before t) -> slippage tier, OKX tier-1 MMR / max leverage (current OKX tiers;
default 2.5% / 20x for coins OKX does not list), point-in-time OKX listing, OKX funding at the same timestamp,
BTC funding at t, BTC 1m window aligned with the coin window.
-> data/ev.parquet (one row per window row) and data/win_btc.npy (float32 [n,122,4])
"""
import json, os
import numpy as np, pandas as pd
from common import D

PRE, NB = 61, 122


def coin_of(sym, okx_coins):
    b = sym[:-4]
    for p in ('1000000', '1000', '1M', '10000'):
        if b.startswith(p) and b[len(p):] in okx_coins:
            return b[len(p):]
    return b


def main():
    meta = pd.read_parquet(os.path.join(D, 'win_meta_perp.parquet'))
    st = pd.read_parquet(os.path.join(D, 'settle.parquet'))
    ev = meta.merge(st[['sym', 't', 'rate', 'interval_h', 'ann', 'rate_prev', 'ann_prev', 'ann_prev2', 'gap_h']],
                    on=['sym', 't'], how='left')
    assert ev.rate.notna().all()
    # ---- BTC funding at t (BTC settles every 8h) ----
    bf = st[st.sym == 'BTCUSDT'][['t', 'rate']].rename(columns={'rate': 'rate_btc'})
    ev = ev.merge(bf, on='t', how='left')
    ev['rate_btc'] = ev.rate_btc.fillna(0.0)
    # ---- 1h features ----
    k = pd.read_parquet(os.path.join(D, 'k1h.parquet'))
    btc = k[k.sym == 'BTCUSDT'].set_index('t').c.astype(float)
    rb = np.log(btc).diff()
    feats = []
    for sym, g in k.groupby('sym'):
        g = g.set_index('t')
        c = g.c.astype(float)
        r = np.log(c).diff()
        rbb = rb.reindex(r.index)
        ok = r.notna() & rbb.notna()
        x, y = rbb.where(ok), r.where(ok)
        n = ok.astype(float).rolling(168, min_periods=1).sum()
        mx = x.rolling(168, min_periods=72).mean(); my = y.rolling(168, min_periods=72).mean()
        cov = (x * y).rolling(168, min_periods=72).mean() - mx * my
        var = (x * x).rolling(168, min_periods=72).mean() - mx * mx
        beta = (cov / var).where(n >= 72)
        vol24 = g.qv.astype(float).rolling(24, min_periods=12).sum()
        rng = ((g.h - g.l) / g.o).astype(float)
        feats.append(pd.DataFrame({'sym': sym, 'hb': g.index.values, 'beta_raw': beta.values, 'vol24': vol24.values}))
    F = pd.concat(feats, ignore_index=True)
    # value usable at t: computed on bars up to the one that OPENED at floor_hour(t) - 2h (closed <= t - 1h)
    ev['hb'] = (ev.t // 3600000) * 3600000 - 2 * 3600000
    ev = ev.merge(F, on=['sym', 'hb'], how='left')
    ev['beta'] = ev.beta_raw.clip(0.2, 3.0).fillna(1.0)
    ev['vol24'] = ev.vol24.fillna(0.0)
    # slippage tier (per side, fraction) from Binance 24h quote volume; +10% of the execution bar range added later
    v = ev.vol24.values
    ev['slip0'] = np.select([v >= 1e9, v >= 2e8, v >= 5e7, v >= 1e7], [3e-4, 3e-4, 5e-4, 8e-4], 15e-4)
    # ---- OKX tiers (current) ----
    tiers = json.load(open(os.path.join(D, 'okx_tier1.json')))
    okx_coins = set(tiers)
    ev['coin'] = [coin_of(s, okx_coins) for s in ev.sym]
    ev['mmr'] = ev.coin.map(lambda c: tiers[c]['mmr'] if c in tiers else 0.025)
    ev['maxlev'] = ev.coin.map(lambda c: tiers[c]['maxLever'] if c in tiers else 20.0)
    ev['on_okx_now'] = ev.coin.isin(okx_coins)
    # ---- point-in-time OKX listing + OKX funding at the same timestamp ----
    of = pd.read_parquet('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/okx_funding_all.parquet')
    of['coin'] = of.instId.str.split('-').str[0]
    of['day'] = of.funding_time // 86400000
    listed_days = set(zip(of.coin, of.day))
    last_okx_file_day = int(of.day.max())
    inst = json.load(open(os.path.join(D, 'okx_instruments.json')))['data']
    lt = {x['instId'].split('-')[0]: int(x['listTime']) for x in inst if x['instId'].endswith('-USDT-SWAP') and x.get('instCategory') == '1'}
    day = (ev.t // 86400000).values
    pit = []
    for c, d, t in zip(ev.coin.values, day, ev.t.values):
        if d <= last_okx_file_day:
            pit.append((c, d) in listed_days or (c, d - 1) in listed_days)
        else:
            pit.append(c in lt and lt[c] <= t - 86400000)
    ev['okx_pit'] = pit
    ev['okx_data'] = day <= last_okx_file_day
    of['t'] = (of.funding_time // 60000) * 60000
    ofr = of[['coin', 't', 'funding_rate']].drop_duplicates(['coin', 't']).rename(columns={'funding_rate': 'okx_rate'})
    ev = ev.merge(ofr, on=['coin', 't'], how='left')
    ev = ev.sort_values('row').reset_index(drop=True)
    # ---- BTC window ----
    b = pd.read_parquet(os.path.join(D, 'btc_1m.parquet'))
    bm = (b.t.values // 60000).astype(np.int64)
    base = bm[0]
    full = np.full((bm[-1] - base + 1, 4), np.nan, dtype=np.float32)
    full[bm - base] = b[['o', 'h', 'l', 'c']].values.astype(np.float32)
    s = (ev.t.values // 60000) - PRE - base
    idx = s[:, None] + np.arange(NB)[None, :]
    idx = np.clip(idx, 0, len(full) - 1)
    WB = full[idx]
    np.save(os.path.join(D, 'win_btc.npy'), WB)
    ev.drop(columns=['hb']).to_parquet(os.path.join(D, 'ev.parquet'))
    print(len(ev), 'events; beta median', ev.beta.median().round(2), 'beta missing->1:', ev.beta_raw.isna().mean().round(3),
          '; on OKX now', ev.on_okx_now.mean().round(3), 'okx point-in-time', ev.okx_pit.mean().round(3),
          '; OKX funding same ts (where data)', ev.okx_rate[ev.okx_data].notna().mean().round(3))
    print(ev.slip0.value_counts(normalize=True).round(3).to_dict())


if __name__ == '__main__':
    main()
