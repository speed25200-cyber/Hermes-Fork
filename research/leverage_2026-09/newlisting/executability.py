"""Executability of the selected config on OKX for a 1k-10k USDT account.
(a) Price proxy: for every trade whose OKX contract is still listed, OKX 1H last-price candles (public REST
    history-candles) replace Binance bars: entry/exit at the OKX open of the same hours, stop on OKX highs.
(b) Lot sizes: contracts = notional / (ctVal * price) rounded DOWN to lotSz; below minSz -> trade impossible.
    Checked for equity 1,000 and 10,000 USDT at L = 1 and L = 2 (notional fraction from the simulator); BTC hedge leg too.
(c) OKX max leverage (current lever) and tier-1 limits of the traded contracts.
-> results/executability.json, results/okx_vs_binance_trades.csv"""
import json, os, sys
sys.path.insert(0, '/home/user/Hermes/src')
from common import *
from hermes.execution.okx.instruments import okx_inst_id, binance_price_factor

OUT = os.path.join(BASE, 'results')
tr = pd.read_csv(os.path.join(OUT, 'trades_selected_binance.csv'), parse_dates=['entry_time', 'exit_time'])
ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
inst = {x['instId']: x for x in json.load(open(os.path.join(D, 'okx_instruments.json')))}
tiers = json.load(open(os.path.join(D, 'okx_tiers.json')))
STOP = 0.5


def okx_candles(iid, t_from, t_to):
    """1H candles with open time in [t_from, t_to] (ms)."""
    rows, after = [], t_to + 3600000
    while True:
        d = okx_get(f'/api/v5/market/history-candles?instId={iid}&bar=1H&limit=100&after={after}')
        if not isinstance(d, list) or not d:
            break
        rows += d
        after = int(d[-1][0])
        if after <= t_from:
            break
        time.sleep(0.12)
    if not rows:
        return None
    df = pd.DataFrame([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in rows], columns=['t', 'o', 'h', 'l', 'c'])
    return df[(df.t >= t_from) & (df.t <= t_to)].drop_duplicates('t').sort_values('t').set_index('t')


rows = []
for _, t in tr.iterrows():
    iid = okx_inst_id(t.sym)
    f = binance_price_factor(t.sym)
    live = iid in inst and inst[iid].get('instCategory') == '1'
    r = dict(sym=t.sym, inst=iid, entry_time=t.entry_time, oos=t.entry_time >= pd.Timestamp('2025-01-01'), bn_ret=t.ret,
             reason=t.reason, notional=t.notional, entry_px=t.entry_px, okx_live_now=live)
    if live:
        x = inst[iid]
        r.update(ctVal=float(x['ctVal']), lotSz=float(x['lotSz']), minSz=float(x['minSz']), lever=float(x['lever']),
                 tier1_maxSz=tiers[iid][0][0] if iid in tiers else np.nan, tier1_mmr=tiers[iid][0][1] if iid in tiers else np.nan)
        a = int(t.entry_time.value // 10 ** 6)
        b = int(t.exit_time.value // 10 ** 6)
        c = okx_candles(iid, a, b)
        if c is not None and a in c.index:
            po = c.loc[a, 'o'] * f
            path = c.loc[a:b]
            stop_px = po * (1 + STOP)
            hit = path[path.h * f >= stop_px]
            if len(hit):
                th = hit.index[0]
                px_exit = max(c.loc[th, 'o'] * f, stop_px)
                reason = 'stop'
            elif b in c.index:
                px_exit = c.loc[b, 'o'] * f
                reason = 'end'
            else:
                px_exit = path.c.iloc[-1] * f
                reason = 'last'
            r.update(okx_entry=po, okx_exit=px_exit, okx_ret=-(px_exit / po - 1), okx_reason=reason,
                     entry_gap_bp=(po / t.entry_px - 1) * 1e4)
        time.sleep(0.12)
    rows.append(r)
X = pd.DataFrame(rows)
X.to_csv(os.path.join(OUT, 'okx_vs_binance_trades.csv'), index=False)
out = {}
for per, m in (('IS', ~X.oos), ('OOS', X.oos)):
    y = X[m]
    z = y[y.okx_ret.notna()]
    out[per] = dict(trades=int(len(y)), okx_live_now=int(y.okx_live_now.sum()), okx_candles=int(len(z)),
                    mean_ret_binance_same=float(z.bn_ret.mean()) if len(z) else None,
                    mean_ret_okx=float(z.okx_ret.mean()) if len(z) else None,
                    corr=float(z[['bn_ret', 'okx_ret']].corr().iloc[0, 1]) if len(z) > 2 else None,
                    median_abs_entry_gap_bp=float(z.entry_gap_bp.abs().median()) if len(z) else None,
                    p90_abs_entry_gap_bp=float(z.entry_gap_bp.abs().quantile(0.9)) if len(z) else None,
                    stop_disagreements=int((z.okx_reason.eq('stop') != z.reason.eq('stop')).sum()) if len(z) else None,
                    mean_abs_ret_diff=float((z.okx_ret - z.bn_ret).abs().mean()) if len(z) else None)
# lot sizes
btc = inst['BTC-USDT-SWAP']
lots = {}
for acct in (1000, 10000):
    for L in (1, 2):
        y = X[X.okx_live_now].copy()
        tgt = y.notional * acct * L        # notional fraction was of equity at entry; equity ~ account here
        unit = y.ctVal * y.entry_px / y.apply(lambda r: binance_price_factor(r.sym), axis=1)   # USDT per contract (OKX units)
        n = np.floor(tgt / unit / y.lotSz + 1e-9) * y.lotSz
        feas = n >= y.minSz
        err = (n * unit - tgt).abs() / tgt
        bunit = float(btc['ctVal']) * 100000.0     # BTC ~ 100k USDT; lot 0.01 contract
        bn = np.floor(tgt / bunit / float(btc['lotSz'])) * float(btc['lotSz'])
        lots[f'{acct}_L{L}'] = dict(n=int(len(y)), feasible=int(feas.sum()), min_target_usdt=float(tgt.min()),
                                    median_contract_usdt=float(unit.median()), max_contract_usdt=float(unit.max()),
                                    median_rounding_err=float(err[feas].median()), p90_rounding_err=float(err[feas].quantile(0.9)),
                                    btc_hedge_min_usdt=float(bunit * float(btc['minSz'])),
                                    btc_hedge_median_rounding_err=float(((bn * bunit - tgt).abs() / tgt).median()))
out['lots'] = lots
y = X[X.okx_live_now]
out['okx_lever'] = dict(min=float(y.lever.min()), median=float(y.lever.median()), frac_ge_10=float((y.lever >= 10).mean()),
                        tier1_mmr_max=float(y.tier1_mmr.max()), tier1_maxSz_usdt_min=float((y.tier1_maxSz * y.ctVal * y.entry_px / y.sym.map(binance_price_factor)).min()))
json.dump(out, open(os.path.join(OUT, 'executability.json'), 'w'), indent=1, default=str)
print(json.dumps(out, indent=1, default=str))
