"""Calendar-basis backtest: short Binance USDT-M quarterly future, long USDT-M perp (both derivatives).

Leverage L = notional per leg / (sub-)account equity, set at entry (and at each roll).
Margin modes:
  cross      : both legs in one cross-margin account; liquidation when equity <= sum_legs (mmr+fee_close)*notional,
               checked on 5m mark-price bars using synchronous (o,o),(h,h),(l,l),(c,c) candidates.
  cross_stress: same but worst non-synchronous combo (future mark high, perp mark low) inside each 5m bar.
  pm_approx  : optimistic portfolio-margin proxy: liquidation only when equity <= fee_close*gross notional.
  isolated   : each leg its own isolated margin (E/2 each); leg liquidated on its own mark high/low; the survivor
               leg is closed at that bar close.
  isolated_rb: isolated + margin rebalancing (partial close/re-open of the winning leg, costs charged) when one
               leg's equity < k * average leg equity.
Liquidation: the liquidated margin is lost entirely (maintenance margin + liquidation fee go to the exchange);
the account continues with whatever equity is left (0 for a cross liquidation).
"""
import os, json, math, itertools, sys
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, 'data')
BAR = pd.Timedelta('5min')
GRID = pd.date_range('2021-12-01', '2026-08-31 23:55', freq='5min')
NB = len(GRID)

COST = dict(fee_taker=0.0005, fee_maker=0.0002, deliv_fee=0.0005, slip_perp=0.0001, slip_fut=0.0003,
            mmr_perp=0.004, mmr_fut=0.005, fee_close=0.0005)


def rd(n):
    d = pd.read_parquet(os.path.join(D, n + '.parquet'))
    d.index = pd.to_datetime(d.t, unit='ms')
    return d


def to_grid(d, cols, ffill=True):
    x = d[cols].reindex(GRID)
    if ffill:
        x = x.ffill()
    return {c: x[c].to_numpy(float) for c in cols}


def load_asset(a):
    sym = a + 'USDT'
    pl = rd(sym + '_perp_last'); pm = rd(sym + '_perp_mark'); ix = rd(sym + '_index'); fu = rd(sym + '_funding')
    A = {'name': a}
    A['P'] = to_grid(pl, ['c'])['c']
    m = to_grid(pm, ['o', 'h', 'l', 'c']); A.update({'Pm' + k: v for k, v in m.items()})
    A['I'] = to_grid(ix, ['c'])['c']
    # funding: event time T (00/08/16 UTC) is charged to positions held at T -> bar whose close == T (open = T-5m)
    fr = np.zeros(NB)
    ft = fu.index.floor('min') - BAR
    pos = GRID.get_indexer(ft)
    ok = pos >= 0
    fr[pos[ok]] = fu.rate.to_numpy()[ok]
    A['fr'] = fr
    A['fund_ann7'] = pd.Series(fu.rate.to_numpy(), index=ft).rolling(21).mean().mul(3 * 365).reindex(GRID).ffill().to_numpy()
    cons = []
    for fn in sorted(os.listdir(D)):
        if not (fn.startswith(sym + '_2') and fn.endswith('_last.parquet')):
            continue
        c = fn.replace('_last.parquet', '')
        exp = pd.Timestamp('20' + c.split('_')[1]) + pd.Timedelta(hours=8)
        if exp < pd.Timestamp('2022-01-15') :
            continue
        fl = rd(c + '_last'); fm = rd(c + '_mark')
        fl = fl[fl.index < exp]; fm = fm[fm.index < exp]
        b0 = GRID.get_indexer([fl.index[0]])[0]
        if b0 < 0:
            b0 = 0
        eb = GRID.get_indexer([exp - BAR])[0]  # last bar (closes at expiry). -1 if beyond grid
        b1 = eb if eb >= 0 else NB - 1
        sl = slice(b0, b1 + 1)
        F = fl.c.reindex(GRID[sl]).ffill().to_numpy()
        mk = fm[['o', 'h', 'l', 'c']].reindex(GRID[sl]).ffill()
        # mark gaps: fall back on last price
        for k in 'ohlc':
            mk[k] = mk[k].fillna(pd.Series(F, index=GRID[sl]))
        settle = np.nan
        if eb >= 0:
            settle = np.nanmean(A['I'][eb - 5: eb + 1])  # 30-min index TWAP (07:30-08:00)
        tau = ((exp - (GRID[sl] + BAR)).total_seconds() / 86400.0).to_numpy()
        prem = F / A['P'][sl] - 1.0
        prem_h = pd.Series(prem).rolling(12, min_periods=6).mean().to_numpy()   # 1h mean of 5m premia
        cons.append(dict(name=c, exp=exp, b0=b0, b1=b1, eb=eb, F=F, Fmo=mk.o.to_numpy(), Fmh=mk.h.to_numpy(),
                         Fml=mk.l.to_numpy(), Fmc=mk.c.to_numpy(), settle=settle, tau=tau, prem=prem_h,
                         qv=fl.qv.reindex(GRID[sl]).fillna(0).to_numpy()))
    A['cons'] = cons
    return A


def bar_of(ts):
    return int(GRID.get_indexer([pd.Timestamp(ts)])[0])


class Sim:
    """One asset, one (sub-)account. side +1 = short quarterly / long perp ; side -1 = long quarterly / short perp."""

    def __init__(self, A, p, L, mode, cost=COST, start='2022-01-01', end='2026-08-31 23:55'):
        self.A, self.p, self.L, self.mode, self.c = A, p, L, mode, dict(cost)
        self.s0, self.s1 = bar_of(start), bar_of(end)
        self.eqc = np.full(NB, np.nan)   # equity at bar close (mark based)
        self.eql = np.full(NB, np.nan)   # intrabar worst equity
        self.trades = []
        self.nliq = 0; self.nrb = 0; self.nroll = 0; self.nentry = 0
        c = self.c
        self.fee_fut_open = c['fee_maker'] if p.get('maker_fut') else c['fee_taker']
        self.slip_fut_open = 0.0 if p.get('maker_fut') else c['slip_fut']
        self.aF = c['mmr_fut'] + c['fee_close']
        self.aP = c['mmr_perp'] + c['fee_close']
        if mode == 'pm_approx':
            self.aF = self.aP = c['fee_close']

    # ---------- signal (precomputed on hourly decision bars) ----------
    def prep(self):
        A, p = self.A, self.p
        dec = np.zeros(NB, bool); dec[GRID.minute.to_numpy() == 55] = True
        dec[:self.s0] = False; dec[self.s1:] = False
        self.dec = dec
        self.decidx = di = np.flatnonzero(dec)
        cons = A['cons']
        S = np.full((len(cons), len(di)), np.nan); TAU = np.full((len(cons), len(di)), np.nan)
        for n, con in enumerate(cons):
            m = (di >= con['b0'] + 12) & (di < con['b1'])
            k = di[m] - con['b0']
            tau = con['tau'][k]; pr = con['prem'][k]
            ann = pr * 365.0 / np.maximum(tau, 1e-9)
            if p['signal'] == 'net':
                fa = A['fund_ann7'][di[m]]
                ann = ann - np.where(np.isfinite(fa), fa, 0.0)
            S[n, m] = ann; TAU[n, m] = tau
        self.S, self.TAU = S, TAU
        with np.errstate(invalid='ignore'):
            okt = TAU >= p['tau_min']
            el_s = (S >= p['h_in']) & okt if p.get('h_in') is not None else np.zeros_like(okt)
            el_r = (S <= p['h_in_rev']) & okt if p.get('h_in_rev') is not None else np.zeros_like(okt)
            th = TAU > p.get('tau_hold', 2.0)
            self.exit_s = (S < p['h_out']) & th if p.get('h_out') is not None else np.zeros_like(okt)
            self.exit_r = (S > p['h_out_rev']) & th if p.get('h_out_rev') is not None else np.zeros_like(okt)
        self.Sel_s = np.where(el_s, S, -np.inf)
        self.Sel_r = np.where(el_r, -S, -np.inf)
        self.elig_pos = np.flatnonzero(el_s.any(0) | el_r.any(0))
        self.cidx = {id(c): n for n, c in enumerate(cons)}

    def best_entry(self, i, exclude=None, side=None):
        d = np.searchsorted(self.decidx, i)
        if d >= len(self.decidx) or self.decidx[d] != i:
            return None
        out = None
        for sd, M in ((1, self.Sel_s), (-1, self.Sel_r)):
            if side is not None and sd != side:
                continue
            col = M[:, d].copy()
            if exclude is not None:
                col[self.cidx[id(exclude)]] = -np.inf
            n = int(np.argmax(col))
            if np.isfinite(col[n]) and out is None:
                out = (self.A['cons'][n], sd * col[n], sd)
        return out

    def next_entry(self, i):
        k = np.searchsorted(self.decidx[self.elig_pos], i)
        if k >= len(self.elig_pos):
            return None
        return int(self.decidx[self.elig_pos[k]])

    # ---------- main loop ----------
    def run(self):
        A, c = self.A, self.c
        self.prep()
        E = 1.0
        i = self.s0
        self.eqc[i] = E; self.eql[i] = E
        pos = None
        while i < self.s1:
            if pos is None:
                j = self.next_entry(i) if E > 1e-6 else None
                found = self.best_entry(j) if j is not None and j < self.s1 - 1 else None
                if found is None:
                    self.eqc[i:self.s1 + 1] = E; self.eql[i:self.s1 + 1] = E
                    break
                self.eqc[i:j + 1] = E; self.eql[i:j + 1] = E
                x = j + 1  # execution bar = next 5m bar close
                con, sv, sd = found
                q = self.L * E / A['P'][x]
                Fe = con['F'][x - con['b0']] * (1 - sd * self.slip_fut_open)
                Pe = A['P'][x] * (1 + sd * c['slip_perp'])
                fee = q * Fe * self.fee_fut_open + q * Pe * c['fee_taker']
                E -= fee
                self.nentry += 1
                pos = dict(con=con, q=q, Fe=Fe, Pe=Pe, E0=E, x=x, sig_in=sv, side=sd, fees=fee)
                self.eqc[x] = E; self.eql[x] = E
                i = x
                continue
            E, pos, i = self.hold(pos, i)
        return self

    def planned_exit(self, pos, i):
        con = pos['con']
        last = min(con['b1'], self.s1)
        n = self.cidx[id(con)]
        d0 = np.searchsorted(self.decidx, i + 1)
        d1 = np.searchsorted(self.decidx, last, side='right')
        em = self.exit_s if pos['side'] == 1 else self.exit_r
        hits = np.flatnonzero(em[n, d0:d1])
        cand = int(self.decidx[d0 + hits[0]]) if len(hits) else None
        if 0 <= con['eb'] <= last and (cand is None or con['eb'] <= cand):
            return con['eb'], 'expiry'
        if cand is not None:
            return cand, 'signal'
        return last, 'end'

    def hold(self, pos, i):
        A, c = self.A, self.c
        con = pos['con']; q = pos['q']; Fe = pos['Fe']; Pe = pos['Pe']; E0 = pos['E0']; sd = pos['side']
        e, why = self.planned_exit(pos, i)
        e_exec = e + 1 if why == 'signal' else e
        e_exec = min(e_exec, self.s1, con['b1'])
        t = np.arange(i + 1, e_exec + 1)
        k = t - con['b0']
        cf = sd * np.cumsum(-A['fr'][t] * q * A['Pmc'][t])  # funding: long perp pays positive rates
        Fm = {s: con['Fm' + s][k] for s in 'ohlc'}
        Pm = {s: A['Pm' + s][t] for s in 'ohlc'}
        mode = self.mode
        if mode in ('cross', 'cross_stress', 'pm_approx'):
            cands = [('o', 'o'), ('h', 'h'), ('l', 'l'), ('c', 'c')]
            if mode == 'cross_stress':
                cands.append(('h', 'l') if sd == 1 else ('l', 'h'))
            eqs = []; marg = []
            for fs, ps in cands:
                eq = E0 + sd * q * ((Fe - Fm[fs]) + (Pm[ps] - Pe)) + cf
                mm = q * (self.aF * Fm[fs] + self.aP * Pm[ps])
                eqs.append(eq); marg.append(eq - mm)
            eqs = np.array(eqs); marg = np.array(marg)
            eqlow = eqs.min(0)
            eqclose = E0 + sd * q * ((Fe - Fm['c']) + (Pm['c'] - Pe)) + cf
            bad = np.nonzero(marg.min(0) <= 0)[0]
            if len(bad):
                b = bad[0]; tb = t[b]
                self.eqc[t[:b]] = eqclose[:b]; self.eql[t[:b]] = eqlow[:b]
                self.eqc[tb] = 0.0; self.eql[tb] = 0.0
                self.nliq += 1
                self.trades.append(dict(con=con['name'], side=sd, entry=str(GRID[pos['x']]), exit=str(GRID[tb]), why='LIQ',
                                        q=q, E0=E0, Eend=0.0, sig_in=pos['sig_in'], notional=q * Pe))
                return 0.0, None, tb
            self.eqc[t] = eqclose; self.eql[t] = eqlow
            return self.close_out(pos, e_exec, why, cf[-1] if len(cf) else 0.0)
        # ---- isolated legs ----
        MF = pos.get('MF', E0 / 2); MP = pos.get('MP', E0 / 2)
        fw = 'h' if sd == 1 else 'l'   # adverse extreme for futures leg
        pw = 'l' if sd == 1 else 'h'   # adverse extreme for perp leg
        rb = mode == 'isolated_rb'
        cfp = cf.copy()
        cur = 0
        while True:
            eqF_w = MF + sd * q * (Fe - Fm[fw][cur:])
            eqP_w = MP + sd * q * (Pm[pw][cur:] - Pe) + cfp[cur:]
            liqF = eqF_w <= self.aF * q * Fm[fw][cur:]
            liqP = eqP_w <= self.aP * q * Pm[pw][cur:]
            eqF_c = MF + sd * q * (Fe - Fm['c'][cur:]); eqP_c = MP + sd * q * (Pm['c'][cur:] - Pe) + cfp[cur:]
            tot_c = eqF_c + eqP_c
            tot_l = np.minimum.reduce([MF + MP + sd * q * ((Fe - Fm[s][cur:]) + (Pm[s][cur:] - Pe)) + cfp[cur:] for s in 'ohlc'])
            tot_l = np.minimum(tot_l, np.minimum(eqF_w, eqF_c) + np.minimum(eqP_w, eqP_c))
            ev = liqF | liqP
            if rb:
                need = np.minimum(eqF_c, eqP_c) < self.p.get('rb_k', 0.5) * tot_c / 2
                ev = ev | need
            idx = np.nonzero(ev)[0]
            if not len(idx):
                tt = t[cur:]
                self.eqc[tt] = tot_c; self.eql[tt] = tot_l
                return self.close_out(dict(pos, E0=MF + MP, Fe=Fe, Pe=Pe), e_exec, why, cfp[-1] if len(cfp) else 0.0, iso=True)
            b = idx[0]; tb = t[cur + b]
            self.eqc[t[cur:cur + b]] = tot_c[:b]; self.eql[t[cur:cur + b]] = tot_l[:b]
            if liqF[b] or liqP[b]:
                self.nliq += 1
                Fx = con['F'][tb - con['b0']]; Px = A['P'][tb]
                Eleft = 0.0; lowv = 0.0
                if not liqF[b]:   # survivor futures leg closed at bar close
                    Eleft += MF + sd * q * (Fe - Fx * (1 + sd * c['slip_fut'])) - q * Fx * c['fee_taker']
                    lowv = eqF_w[b]
                if not liqP[b]:   # survivor perp leg closed at bar close
                    Eleft += MP + sd * q * (Px * (1 - sd * c['slip_perp']) - Pe) + cfp[cur + b] - q * Px * c['fee_taker']
                    lowv = eqP_w[b]
                Eleft = max(Eleft, 0.0)
                self.eqc[tb] = Eleft; self.eql[tb] = min(Eleft, max(lowv, 0.0))
                self.trades.append(dict(con=con['name'], side=sd, entry=str(GRID[pos['x']]), exit=str(GRID[tb]),
                                        why='LIQ_' + ('F' if liqF[b] else 'P'), q=q, E0=E0, Eend=Eleft, sig_in=pos['sig_in'],
                                        notional=q * Pe))
                return Eleft, None, tb
            # margin rebalance: partially close + re-open the winning leg to realise and move profit
            eF, eP = eqF_c[b], eqP_c[b]
            if eF >= eP:
                T = (eF - eP) / 2; px = Fm['c'][cur + b]; cst = 2 * (T / eF) * q * px * (c['fee_taker'] + c['slip_fut'])
            else:
                T = (eP - eF) / 2; px = Pm['c'][cur + b]; cst = 2 * (T / eP) * q * px * (c['fee_taker'] + c['slip_perp'])
            tot = eF + eP - cst
            self.nrb += 1
            MF = MP = tot / 2
            Fe = Fm['c'][cur + b]; Pe = Pm['c'][cur + b]
            cfp = cfp - cfp[cur + b]
            self.eqc[tb] = tot; self.eql[tb] = min(tot_l[b], tot)
            cur = cur + b + 1
            if cur >= len(t):
                return self.close_out(dict(pos, E0=MF + MP, Fe=Fe, Pe=Pe), e_exec, why, 0.0, iso=True)

    def close_out(self, pos, e, why, cf_end, iso=False):
        A, c = self.A, self.c
        con = pos['con']; q = pos['q']; Fe = pos['Fe']; Pe = pos['Pe']; E0 = pos['E0']; sd = pos['side']
        if why == 'expiry':
            Fx = con['settle']; ffee = q * Fx * c['deliv_fee']
            Ptw = np.nanmean(A['P'][e - 5:e + 1])     # perp closed/marked by TWAP over the settlement window
        else:
            Fx = con['F'][e - con['b0']] * (1 + sd * c['slip_fut']); ffee = q * Fx * c['fee_taker']
            Ptw = A['P'][e]
        roll = None
        if why == 'expiry' and self.p.get('roll', True) and e < self.s1:
            roll = self.best_entry(e, exclude=con, side=sd)
        Efut = E0 + sd * q * (Fe - Fx) - ffee
        rec = dict(con=con['name'], side=sd, entry=str(GRID[pos['x']]), exit=str(GRID[e]), q=q, E0=pos['E0'],
                   sig_in=pos['sig_in'], fund=cf_end, fut_pnl=sd * q * (Fe - Fx), notional=q * Pe,
                   prem_in=(pos['Fe'] / pos['Pe'] - 1))
        if roll is not None:
            Pnow = Ptw
            E = Efut + sd * q * (Pnow - Pe) + cf_end
            con2, sv, _ = roll
            q2 = self.L * E / Pnow
            dq = (q + q2) if iso else abs(q2 - q)   # isolated: perp leg re-traded to re-split margin
            Fe2 = con2['F'][e - con2['b0']] * (1 - sd * self.slip_fut_open)
            fee = q2 * Fe2 * self.fee_fut_open + dq * Pnow * (c['fee_taker'] + c['slip_perp'])
            E -= fee
            rec.update(why='roll', Eend=E + fee, perp_pnl=sd * q * (Pnow - Pe))
            self.trades.append(rec)
            self.nroll += 1
            self.eqc[e] = E; self.eql[e] = min(self.eql[e], E) if np.isfinite(self.eql[e]) else E
            return E, dict(con=con2, q=q2, Fe=Fe2, Pe=Pnow, E0=E, x=e, sig_in=sv, side=sd, fees=fee), e
        Px = Ptw * (1 - sd * c['slip_perp'])
        E = Efut + sd * q * (Px - Pe) + cf_end - q * Px * c['fee_taker']
        rec.update(why=why, Eend=E, perp_pnl=sd * q * (Px - Pe))
        self.trades.append(rec)
        self.eqc[e] = E; self.eql[e] = min(self.eql[e], E) if np.isfinite(self.eql[e]) else E
        return E, None, e


def equity_series(sims, weights, start, end):
    s0, s1 = bar_of(start), bar_of(end)
    ec = 0; el = 0
    for s, w in zip(sims, weights):
        a = pd.Series(s.eqc[s0:s1 + 1], index=GRID[s0:s1 + 1]).ffill()
        b = pd.Series(s.eql[s0:s1 + 1], index=GRID[s0:s1 + 1]).fillna(a)
        ec = ec + w * a; el = el + w * np.minimum(a, b)
    return ec, el


def metrics_from(ec, el):
    E0 = ec.iloc[0]; E1 = ec.iloc[-1]
    days = (ec.index[-1] + BAR - ec.index[0]).total_seconds() / 86400
    cagr = (E1 / E0) ** (365.25 / days) - 1 if E1 > 0 else -1.0
    peak = ec.cummax()
    dd = float((np.minimum(el, ec) / peak - 1).min())
    daily = ec.resample('1D').last()
    daily = pd.concat([pd.Series([E0], index=[ec.index[0] - pd.Timedelta('1D')]), daily])
    r = daily.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sh = float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0
    yr = {}
    prev = E0
    for ts, v in ec.resample('YE').last().items():
        yr[str(ts.year)] = (v / prev - 1) if prev > 0 else -1.0
        prev = v
    return dict(cagr=float(cagr), maxdd=dd, sharpe=sh, worst_day=float(r.min()), final=float(E1 / E0), years=yr)


def run_assets(AS, p, L, mode, start, end, cost=COST, weights=None):
    sims = [Sim(A, p, L, mode, cost, start, end).run() for A in AS]
    w = weights or [1.0 / len(AS)] * len(AS)
    ec, el = equity_series(sims, w, start, end)
    m = metrics_from(ec, el)
    m.update(nliq=sum(s.nliq for s in sims), nentry=sum(s.nentry for s in sims), nroll=sum(s.nroll for s in sims),
             nrb=sum(s.nrb for s in sims))
    return sims, m, ec


def load_all(cache=os.path.join(HERE, 'assets.pkl')):
    """BTC and ETH asset bundles (cached as a pickle if present; rebuilt from data/*.parquet otherwise)."""
    import pickle
    if os.path.exists(cache):
        return pickle.load(open(cache, 'rb'))
    return {a: load_asset(a) for a in ['BTC', 'ETH']}
