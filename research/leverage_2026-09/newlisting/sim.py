"""Hourly portfolio simulator for new-listing trades on OKX USDT perps (prices: Binance 1h bars as proxy; funding: OKX
realized funding where the OKX contract has records, else Binance).

One position per listing event: enter at the OPEN of the bar t0 + d0 (+lat), exit at the open of t0 + d1 (+lat),
earlier on a stop or if the contract stops trading (exit at the last close). Eligible only if OKX listed the
contract at the entry hour (archive day and previous archive day exist) and price data exist.
Sizing: at entry, coin-leg notional = equity * L * (1/K) * clip(sigma_ref / sigma_i, 0.25, 1) (sigma_i: realized
daily vol of the coin since listing, >= 12 hourly bars, else scale 0.5), capped so that total coin-leg gross <= L *
equity. Optional BTC hedge: beta * notional in the opposite direction, opened and closed with the coin leg.
Costs per fill: taker fee 5 bp + slippage (10 bp coin legs, 1 bp BTC), times cost_mult. Stops trigger on the intrabar
extreme; fill at max(open, stop) (+slippage) for shorts (gap-through handled), or at the next bar's open with lat>0.
Cross margin: each hour the intrabar-worst equity (every leg at its adverse extreme simultaneously, i.e. highs for
shorts, lows for longs) is compared with the maintenance margin (tier-1 MMR 5% coins, 0.5% BTC, at the adverse
price); equity <= maintenance => liquidation (equity set to 0, sim stops). Funding: position pays qty*price*rate.
Quantities are held constant between entry and exit (no rebalancing).
"""
import os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(BASE, 'data')
HMS = 3600000
G0 = int(pd.Timestamp('2021-12-01').value // 10 ** 6)       # global hour 0 (ms)
IS_START, OOS_START, OOS_END = '2022-01-01', '2025-01-01', '2026-09-01'
FEE, SLIP_COIN, SLIP_BTC = 0.0005, 0.0010, 0.0001
MMR_COIN, MMR_BTC = 0.05, 0.005
STABLE = ['USDCUSDT']


def gh(ts):
    return int((pd.Timestamp(ts).value // 10 ** 6 - G0) // HMS)


class Data:
    def __init__(self, price='hybrid'):
        """price='binance': Binance 1h bars for every event; 'hybrid': OKX 1H candles wherever OKX serves them
        (contracts still listed on OKX), Binance bars otherwise (contracts delisted from OKX since)."""
        self.ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
        P = np.load(os.path.join(D, 'panel2.npz'))
        self.o, self.h, self.l, self.c = P['o'], P['h'], P['l'], P['c']
        self.price = price
        if price == 'hybrid':
            Q = np.load(os.path.join(D, 'panel_okx.npz'))
            has = np.isfinite(Q['o']) & np.isfinite(Q['h']) & np.isfinite(Q['l']) & np.isfinite(Q['c'])
            self.o, self.h, self.l, self.c = (np.where(has, Q[k], x) for k, x in
                                              (('o', self.o), ('h', self.h), ('l', self.l), ('c', self.c)))
            self.src_okx = has
        self.bo, self.bh, self.bl, self.bc = P['bo'], P['bh'], P['bl'], P['bc']
        self.fund = np.where(np.isfinite(P['fund_okx']), P['fund_okx'], P['fund'])   # a few empty OKX records
        self.okx_on = P['okx_on'].copy()
        self.okx_on[self.ev.sym.isin(STABLE).values] = False                         # stablecoin perps
        self.n, self.H = self.o.shape
        self.g0 = ((self.ev.t0.values - G0) // HMS).astype(np.int64)
        t0 = pd.to_datetime(self.ev.t0, unit='ms')
        self.newtok = (self.ev.spot_first.isna() | ((t0 - self.ev.spot_first).dt.days <= 30)).values
        self.year = t0.dt.year.values
        # realized daily vol since listing, available at each local hour (bars 1..k-1)
        lr = np.diff(np.log(self.c), axis=1)
        lr[:, 0] = np.nan                              # skip the listing bar
        m = np.isfinite(lr)
        x = np.where(m, lr, 0.0)
        cs, cs2, cn = np.cumsum(x, 1), np.cumsum(x * x, 1), np.cumsum(m, 1)
        var = (cs2 - cs ** 2 / np.maximum(cn, 1)) / np.maximum(cn - 1, 1)
        self.vol_d = np.sqrt(np.maximum(var, 0) * 24)  # vol_d[:, k-1] uses bars up to k (i.e. before bar k+1 open)
        self.vol_n = cn
        isv = (self.year >= 2022) & (self.year <= 2024)
        self.sigma_ref = float(np.nanmedian(self.vol_d[isv, 71]))   # IS only: median daily vol over first 3 days


def simulate(Dt, cfg, start=IS_START, end=OOS_START, L=1.0, cost_mult=1.0, lat=0, exclude=None,
             min_frac=0.0, record=False):
    """cfg: dict(d0 hours, d1 hours, side -1/+1, beta, stop (None or frac), uni 'okx'|'newtok', K)"""
    d0, d1, side, beta, stop, K = cfg['d0'], cfg['d1'], cfg['side'], cfg['beta'], cfg['stop'], cfg['K']
    gs, ge = gh(start), gh(end)
    fee_c = (FEE + SLIP_COIN) * cost_mult
    fee_b = (FEE + SLIP_BTC) * cost_mult
    ent = Dt.g0 + d0 + lat
    elig = (ent >= gs) & (ent < ge) & (d0 + lat < Dt.H)
    if cfg['uni'] == 'newtok':
        elig &= Dt.newtok
    idx = np.where(elig)[0]
    ok = []
    for i in idx:
        k = d0 + lat
        if Dt.okx_on[i, k] and np.isfinite(Dt.o[i, k]) and np.isfinite(Dt.bo[i, k]):
            if exclude is not None and Dt.ev.sym.values[i] in exclude:
                continue
            ok.append(i)
    entries = {}
    for i in ok:
        entries.setdefault(int(ent[i]), []).append(i)
    E = 1.0
    peak = 1.0
    maxdd = 0.0
    liq = False
    pos = []   # dict(i, q, last, entry, bq, blast, xt, stop_px, pending_stop)
    eq_close = np.full(ge - gs, np.nan)
    eq_worst = np.full(ge - gs, np.nan)
    gross_rec = np.zeros(ge - gs)
    trades = []
    t_first = min(entries) if entries else ge
    eq_close[:max(t_first - gs, 0)] = 1.0
    eq_worst[:max(t_first - gs, 0)] = 1.0
    for t in range(max(t_first, gs), ge):
        # 1) exits at the open of bar t (scheduled, pending stops, or data ended)
        keep = []
        for p in pos:
            k = t - Dt.g0[p['i']]
            px = Dt.o[p['i'], k] if k < Dt.H else np.nan
            if t >= p['xt'] or p['pending'] or not np.isfinite(px):
                if not np.isfinite(px):
                    px = p['last']
                E += p['q'] * (px - p['last']) - abs(p['q']) * px * fee_c
                bpx = Dt.bo[p['i'], k] if k < Dt.H and np.isfinite(Dt.bo[p['i'], k]) else p['blast']
                E += p['bq'] * (bpx - p['blast']) - abs(p['bq']) * bpx * fee_b
                p['tr']['exit_px'] = px
                p['tr']['exit_t'] = t
                p['tr']['ret'] = side * (px / p['entry'] - 1)
                p['tr']['reason'] = 'stop' if p['pending'] else ('end' if t >= p['xt'] else 'nodata')
                trades.append(p['tr'])
            else:
                keep.append(p)
        pos = keep
        # 2) entries at the open of bar t
        for i in entries.get(t, []):
            k = t - Dt.g0[i]
            px, bpx = Dt.o[i, k], Dt.bo[i, k]
            if Dt.vol_n[i, k - 2] >= 12 if k >= 2 else False:
                scale = np.clip(Dt.sigma_ref / max(Dt.vol_d[i, k - 2], 1e-9), 0.25, 1.0)
            else:
                scale = 0.5
            gross = sum(abs(p['q']) * p['last'] for p in pos)
            notional = min(E * L * scale / K, max(E * L - gross, 0.0))
            if notional <= E * L * min_frac or notional <= 0:
                continue
            q = side * notional / px
            bq = -side * beta * notional / bpx
            E -= abs(q) * px * fee_c + abs(bq) * bpx * fee_b
            sp = None
            if stop is not None:
                sp = px * (1 + stop) if side < 0 else px * (1 - stop)
            tr = dict(sym=Dt.ev.sym.values[i], i=int(i), entry_t=t, entry_px=px, notional=notional, E_before=E)
            pos.append(dict(i=i, q=q, last=px, entry=px, bq=bq, blast=bpx, xt=Dt.g0[i] + d1 + lat, stop_px=sp,
                            pending=False, tr=tr))
        # 3) intrabar worst case, stops, liquidation
        worst = E
        mm = 0.0
        stop_fills = []
        for p in pos:
            k = t - Dt.g0[p['i']]
            o_, h_, l_ = Dt.o[p['i'], k], Dt.h[p['i'], k], Dt.l[p['i'], k]
            if not np.isfinite(h_):
                continue
            adv = h_ if p['q'] < 0 else l_
            if p['stop_px'] is not None and not p['pending']:
                hit = (h_ >= p['stop_px']) if p['q'] < 0 else (l_ <= p['stop_px'])
                if hit:
                    if lat == 0:
                        fill = max(o_, p['stop_px']) if p['q'] < 0 else min(o_, p['stop_px'])
                        adv = fill
                        stop_fills.append((p, fill))
                    else:
                        p['pending'] = True
            worst += p['q'] * (adv - p['last'])
            mm += abs(p['q']) * adv * MMR_COIN
            bo_, bh_, bl_ = Dt.bo[p['i'], k], Dt.bh[p['i'], k], Dt.bl[p['i'], k]
            if np.isfinite(bh_) and p['bq'] != 0:
                badv = bl_ if p['bq'] > 0 else bh_
                worst += p['bq'] * (badv - p['blast'])
                mm += abs(p['bq']) * badv * MMR_BTC
        dd = 1 - worst / peak
        maxdd = max(maxdd, dd)
        eq_worst[t - gs] = worst
        if pos and worst <= mm:
            liq = True
            eq_close[t - gs:] = 0.0
            eq_worst[t - gs:] = 0.0
            maxdd = 1.0
            break
        # stop exits within bar t (lat == 0)
        for p, fill in stop_fills:
            k = t - Dt.g0[p['i']]
            E += p['q'] * (fill - p['last']) - abs(p['q']) * fill * fee_c
            bpx = Dt.bc[p['i'], k] if np.isfinite(Dt.bc[p['i'], k]) else p['blast']
            E += p['bq'] * (bpx - p['blast']) - abs(p['bq']) * bpx * fee_b
            p['tr'].update(exit_px=fill, exit_t=t, ret=side * (fill / p['entry'] - 1), reason='stop')
            trades.append(p['tr'])
        if stop_fills:
            sf = {id(p) for p, _ in stop_fills}
            pos = [p for p in pos if id(p) not in sf]
        # 4) mark to close, funding
        for p in pos:
            k = t - Dt.g0[p['i']]
            c_ = Dt.c[p['i'], k]
            if np.isfinite(c_):
                E += p['q'] * (c_ - p['last'])
                p['last'] = c_
                E -= p['q'] * c_ * Dt.fund[p['i'], k]
            bc_ = Dt.bc[p['i'], k]
            if np.isfinite(bc_) and p['bq'] != 0:
                E += p['bq'] * (bc_ - p['blast'])
                p['blast'] = bc_
        if not np.isfinite(E):
            raise FloatingPointError(f'non-finite equity at hour {t}')
        eq_close[t - gs] = E
        peak = max(peak, E)
        if record:
            gross_rec[t - gs] = sum(abs(p['q']) * p['last'] + abs(p['bq']) * p['blast'] for p in pos) / max(E, 1e-12)
        if E <= 0:
            liq = True
            eq_close[t - gs:] = 0.0
            maxdd = 1.0
            break
    # open positions at the end: marked at the last close (already in E)
    for p in pos:
        p['tr'].update(exit_px=p['last'], exit_t=ge, ret=side * (p['last'] / p['entry'] - 1), reason='open_at_end')
        trades.append(p['tr'])
    idx_h = pd.date_range(start, periods=ge - gs, freq='h')
    eq = pd.Series(eq_close, idx_h).ffill().fillna(1.0)
    daily = eq.resample('D').last()
    ret = daily.pct_change().fillna(daily.iloc[0] - 1.0)
    out = dict(ret=ret, maxdd=maxdd, liq=liq, trades=trades, final=float(eq.iloc[-1]))
    if record:
        out['eq'] = eq
        out['eq_worst'] = pd.Series(eq_worst, idx_h)
        out['gross'] = pd.Series(gross_rec, idx_h)
    return out


def sharpe(r):
    r = np.asarray(r, float)
    s = r.std(ddof=1)
    return float(r.mean() / s * np.sqrt(365)) if s > 0 else 0.0


def cagr(r):
    r = np.asarray(r, float)
    g = np.prod(1 + r)
    return float(g ** (365 / len(r)) - 1) if g > 0 else -1.0


def summarize(res):
    r = res['ret']
    y = r.groupby(r.index.year).apply(lambda x: float(np.prod(1 + x) - 1))
    return dict(sharpe=sharpe(r), cagr=cagr(r), maxdd=res['maxdd'], liq=res['liq'], trades=len(res['trades']),
                vol=float(r.std() * np.sqrt(365)), years={int(k): v for k, v in y.items()})
