"""Statistical-lens verification helpers (reviewer copy; author files untouched).
- load(): Binance events (newlisting sim.Data('hybrid')) + OKX-extra events (xlist/data/sim), merged as in combo.py,
  with per-event metadata: src, base (cluster key), cls, bn_g (global hour of the Binance UM-perp listing, inf if none).
- sim_live(): copy of review_sleeve/livesim.simulate_live with three optional ex-post hooks:
    w[i]          notional multiplier per event (half-weight variant)
    entry_before[i]  global hour; an entry at t is allowed only if t < entry_before[i] (no-Binance-at-entry variant)
    force_xt[i]   global hour; scheduled exit = min(g0+d1, force_xt[i]) (Binance t0 - 24h upper-bound variant)
  and per-trade net PnL / notional (coin + BTC hedge + fees + funding) recorded as 'net'.
With all hooks at default it must reproduce simulate_live bit-for-bit (checked in s0_repro.py)."""
import sys, types
import numpy as np
import pandas as pd
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve')
sys.path.insert(0, SP + '/newlisting')
sys.path.insert(0, '/home/user/Hermes/src')
import sim
from sim import gh, FEE, SLIP_COIN, SLIP_BTC, G0, HMS
from livesim import sharpe
from hermes.data.universe import base_asset
H = 240
SREF = 0.1238230231575359
OUT = SP + '/xlist/verify/stat'


def load():
    D0 = sim.D
    Db = sim.Data('hybrid')
    sim.D = f'{SP}/xlist/data/sim'
    Do = sim.Data('binance')
    sim.D = D0
    Db.sigma_ref = Do.sigma_ref = SREF
    return Db, Do


def merge(a, b, keep_a=None, keep_b=None):
    m = types.SimpleNamespace()
    ka = np.ones(a.n, bool) if keep_a is None else keep_a
    kb = np.ones(b.n, bool) if keep_b is None else keep_b
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka, :H], getattr(b, k)[kb, :H]])
    for k in ('vol_d', 'vol_n'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka, :H - 1], getattr(b, k)[kb, :H - 1]])
    for k in ('g0', 'newtok', 'year'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka], getattr(b, k)[kb]])
    ea = a.ev.loc[ka, ['sym', 't0']].assign(src='binance', cls='binance',
                                            base=[base_asset(s) for s in a.ev.sym.values[ka]], bn_first=np.nan)
    ea['bn_first'] = ea.t0.astype(float)
    eb = b.ev.loc[kb, ['sym', 't0', 'cls', 'bn_first']].assign(src='okx', base=[s[:-len('-USDT-SWAP')] for s in b.ev.sym.values[kb]])
    m.ev = pd.concat([ea, eb], ignore_index=True)
    m.n, m.H = len(m.ev), H
    m.sigma_ref = SREF
    bnf = m.ev.bn_first.values.astype(float)
    m.bn_g = np.where(np.isfinite(bnf), np.floor((bnf - G0) / HMS), np.inf)   # global hour of Binance perp listing
    return m


def take(m, idx):
    """Event subset / resample (with repetition) of a merged namespace."""
    r = types.SimpleNamespace()
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on', 'vol_d', 'vol_n', 'g0', 'newtok', 'year', 'bn_g'):
        r.__dict__[k] = getattr(m, k)[idx]
    r.ev = m.ev.iloc[idx].reset_index(drop=True)
    r.n, r.H, r.sigma_ref = len(idx), m.H, m.sigma_ref
    return r


def sim_live(Dt, tranches=(24, 72), d1=168, stop=0.5, L=1.0, K=5, late=True, shared_stop=True, start='2022-01-01',
             end='2025-01-01', cost_mult=1.0, max_late=2, slot_cap=True, w=None, entry_before=None, force_xt=None):
    gs, ge = gh(start), gh(end)
    fee_c = (FEE + SLIP_COIN) * cost_mult
    fee_b = (FEE + SLIP_BTC) * cost_mult
    nT = len(tranches)
    w = np.ones(Dt.n) if w is None else w
    eb_ = np.full(Dt.n, np.inf) if entry_before is None else entry_before
    fx = np.full(Dt.n, np.inf) if force_xt is None else force_xt
    syms = Dt.ev.sym.values
    ev_idx = np.where(Dt.newtok)[0]
    cands = []
    for i in ev_idx:
        for k, h in enumerate(tranches):
            a = Dt.g0[i] + h
            b = Dt.g0[i] + d1 if late else a + 1
            if max_late is not None and late:
                b = min(b, a + max_late + 1)
            if b <= gs or a >= ge:
                continue
            cands.append((int(i), k, int(a), int(b)))
    by_start = {}
    for c in cands:
        by_start.setdefault(c[2], []).append(c)
    active = []
    taken = set()
    E = 1.0
    pos = []
    trades = []
    eq_close = np.full(ge - gs, np.nan)
    first = min(by_start) if by_start else ge
    eq_close[:max(first - gs, 0)] = 1.0
    stats = dict(late_entries=0, entries=0, stops=0, blocked_bn=0)
    for t in range(max(first, gs), ge):
        keep = []
        for p in pos:
            k = t - Dt.g0[p['i']]
            px = Dt.o[p['i'], k] if k < Dt.H else np.nan
            if t >= p['xt'] or not np.isfinite(px):
                if not np.isfinite(px):
                    px = p['last']
                d = p['q'] * (px - p['last']) - abs(p['q']) * px * fee_c
                bpx = Dt.bo[p['i'], k] if k < Dt.H and np.isfinite(Dt.bo[p['i'], k]) else p['blast']
                d += p['bq'] * (bpx - p['blast']) - abs(p['bq']) * bpx * fee_b
                E += d
                p['pnl'] += d
                p['tr'].update(exit_t=t, ret=-(px / p['entry'] - 1), reason='end' if t >= p['xt'] else 'nodata',
                               net=p['pnl'] / p['tr']['notional'])
                trades.append(p['tr'])
            else:
                keep.append(p)
        pos = keep
        active += by_start.get(t, [])
        active = [c for c in active if t < c[3] and (c[0], c[1]) not in taken]
        due = sorted(active, key=lambda c: (syms[c[0]], c[1]))
        for (i, kk, a, b) in due:
            k = t - Dt.g0[i]
            if k >= Dt.H or t < gs:
                continue
            ok = Dt.okx_on[i, k] and np.isfinite(Dt.o[i, k]) and np.isfinite(Dt.bo[i, k])
            if not ok:
                continue
            if t >= eb_[i]:
                stats['blocked_bn'] += 1
                taken.add((i, kk))             # known ex-ante: never enter this tranche
                continue
            open_listings = {p['i'] for p in pos}
            if slot_cap and i not in open_listings and len(open_listings) >= K:
                continue
            px, bpx = Dt.o[i, k], Dt.bo[i, k]
            if k >= 2 and Dt.vol_n[i, k - 2] >= 12:
                scale = np.clip(Dt.sigma_ref / max(Dt.vol_d[i, k - 2], 1e-9), 0.25, 1.0)
            else:
                scale = 0.5
            gross = sum(abs(p['q']) * p['last'] for p in pos)
            notional = min(E * L * scale * w[i] / K / nT, max(E * L - gross, 0.0))
            if notional <= 0:
                continue
            q = -notional / px
            bq = notional / bpx
            f = abs(q) * px * fee_c + abs(bq) * bpx * fee_b
            E -= f
            sib = [p['stop_px'] for p in pos if p['i'] == i and p['stop_px'] is not None] if shared_stop else []
            sp = sib[0] if sib else (px * (1 + stop) if stop is not None else None)
            if t > a:
                stats['late_entries'] += 1
            stats['entries'] += 1
            taken.add((i, kk))
            tr = dict(sym=syms[i], i=int(i), tranche=kk, entry_t=t, entry_px=px, notional=notional, E_before=E)
            pos.append(dict(i=i, q=q, last=px, entry=px, bq=bq, blast=bpx, xt=min(Dt.g0[i] + d1, fx[i]), stop_px=sp,
                            tr=tr, pnl=-f))
        hit_listings = {}
        for p in pos:
            k = t - Dt.g0[p['i']]
            o_, h_ = Dt.o[p['i'], k], Dt.h[p['i'], k]
            if p['stop_px'] is not None and np.isfinite(h_) and h_ >= p['stop_px']:
                fill = max(o_, p['stop_px'])
                if shared_stop:
                    hit_listings[p['i']] = max(hit_listings.get(p['i'], 0.0), fill)
                else:
                    p['_fill'] = fill
        closed = []
        for p in pos:
            k = t - Dt.g0[p['i']]
            fill = hit_listings.get(p['i']) if shared_stop else p.pop('_fill', None)
            if fill is None:
                continue
            d = p['q'] * (fill - p['last']) - abs(p['q']) * fill * fee_c
            bpx = Dt.bc[p['i'], k] if np.isfinite(Dt.bc[p['i'], k]) else p['blast']
            d += p['bq'] * (bpx - p['blast']) - abs(p['bq']) * bpx * fee_b
            E += d
            p['pnl'] += d
            p['tr'].update(exit_t=t, ret=-(fill / p['entry'] - 1), reason='stop', net=p['pnl'] / p['tr']['notional'])
            trades.append(p['tr'])
            closed.append(id(p))
            stats['stops'] += 1
        pos = [p for p in pos if id(p) not in set(closed)]
        for p in pos:
            k = t - Dt.g0[p['i']]
            c_ = Dt.c[p['i'], k]
            if np.isfinite(c_):
                d = p['q'] * (c_ - p['last'])
                p['last'] = c_
                d -= p['q'] * c_ * Dt.fund[p['i'], k]
                E += d
                p['pnl'] += d
            bc_ = Dt.bc[p['i'], k]
            if np.isfinite(bc_):
                d = p['bq'] * (bc_ - p['blast'])
                E += d
                p['pnl'] += d
                p['blast'] = bc_
        eq_close[t - gs] = E
    for p in pos:
        p['tr'].update(exit_t=ge, ret=-(p['last'] / p['entry'] - 1), reason='open_at_end', net=p['pnl'] / p['tr']['notional'])
        trades.append(p['tr'])
    idx_h = pd.date_range(start, periods=ge - gs, freq='h')
    eq = pd.Series(eq_close, idx_h).ffill().fillna(1.0)
    daily = eq.resample('D').last()
    ret = daily.pct_change().fillna(daily.iloc[0] - 1.0)
    return dict(ret=ret, trades=trades, stats=stats, final=float(eq.iloc[-1]), maxdd=float((1 - eq / eq.cummax()).max()))


def gtime(g):
    return pd.Timestamp('2021-12-01') + pd.to_timedelta(np.asarray(g), unit='h')


def maxdd_ret(r):
    eq = np.cumprod(1 + np.asarray(r))
    return float((1 - eq / np.maximum.accumulate(eq)).max())


PERIODS = [('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-01'),
           ('2026 Jan-Aug', '2026-01-01', '2026-09-01'), ('2022-2026', '2022-01-01', '2026-09-01')]
