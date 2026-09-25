"""Independent re-implementation of the live listing sleeve (hermes/live/listing_sleeve.py) on hourly research panels.
Conventions shared with research: decisions at hourly bar opens, entry at o[k], stop fill max(o, stop) within the bar,
BTC leg closed at bar close on a stop, exit at the open of t0+168h, research costs. Extras (off by default):
kill rule (last 25 closed trades mean pnl/notional <= 0 or loss > kill_loss * NAV * L), BTC hedge funding."""
import numpy as np, pandas as pd
G0 = int(pd.Timestamp('2021-12-01').value // 10 ** 6)
HMS = 3600000
FC, FB = 0.0015, 0.0006


def gh(ts):
    return int((pd.Timestamp(ts).value // 10 ** 6 - G0) // HMS)


def btc_funding_hourly(path, ge):
    f = pd.read_parquet(path)
    arr = np.zeros(ge + 1)
    idx = ((f.funding_time.values - G0) // HMS).astype(int) - 1   # settled in (t, t+1h] -> hour t (as panel.py)
    idx = np.ceil((f.funding_time.values - G0) / HMS).astype(int) - 1
    ok = (idx >= 0) & (idx <= ge)
    np.add.at(arr, idx[ok], f.rate.values[ok])
    return arr


def sim(D, start, end, entries=(24, 72), grace=3, exit_h=168, stop=0.5, K=5, L=1.0, sigma_ref=0.1238230231575359,
        kill_n=0, kill_loss=0.10, btc_fund=None, cost_mult=1.0, lag=None):
    gs, ge = gh(start), gh(end)
    fc, fb = FC * cost_mult, FB * cost_mult
    syms = D.ev.sym.values
    new = np.where(D.newtok)[0]
    # due windows per hour
    win = {}
    for i in new:
        for k, h in enumerate(entries):
            lg = 0 if lag is None else int(lag[i])
            a = int(D.g0[i]) + h + lg
            b = min(a + grace, int(D.g0[i]) + exit_h + lg)
            for t in range(max(a, gs), min(b, ge)):
                win.setdefault(t, []).append((syms[i], k, int(i)))
    E = 1.0
    openp = []           # dict: i, k, q, bq, e_px, e_bpx, last, blast, stop, due, pnl (accumulated realized incl costs/funding), notional
    done = []
    taken = set()
    killed_at = None
    eq = np.full(ge - gs, np.nan)
    ts = sorted(win)
    tstart = ts[0] if ts else ge
    eq[:max(tstart - gs, 0)] = 1.0

    def close(p, px, bpx, t, reason):
        nonlocal E
        dq = p['q'] * (px - p['last']) + p['bq'] * (bpx - p['blast'])
        cost = abs(p['q']) * px * fc + abs(p['bq']) * bpx * fb
        E += dq - cost
        p['pnl'] += dq - cost
        done.append(dict(sym=syms[p['i']], i=p['i'], tranche=p['k'], entry_t=p['t_in'], exit_t=t, ret=-(px / p['e_px'] - 1),
                         notional=p['notional'], pnl=p['pnl'], nav=E, reason=reason))

    def suspended():
        if kill_n <= 0 or len(done) < kill_n:
            return False
        last = done[-kill_n:]
        mean = np.mean([d['pnl'] / d['notional'] for d in last])
        nav = last[-1]['nav']
        loss = -sum(d['pnl'] for d in last) / (nav * L)
        return mean <= 0 or loss > kill_loss

    for t in range(max(tstart, gs), ge):
        # exits at open
        keep = []
        for p in openp:
            k = t - D.g0[p['i']]
            px = D.o[p['i'], k] if k < D.H else np.nan
            if t >= p['due'] or not np.isfinite(px):
                px = px if np.isfinite(px) else p['last']
                bpx = D.bo[p['i'], k] if k < D.H and np.isfinite(D.bo[p['i'], k]) else p['blast']
                close(p, px, bpx, t, 'end')
            else:
                keep.append(p)
        openp = keep
        # entries
        sus = suspended()
        if sus and killed_at is None:
            killed_at = t
        if not sus:
            for sym, kk, i in sorted(win.get(t, [])):
                if (i, kk) in taken:
                    continue
                k = t - D.g0[i]
                if k >= D.H or not (D.okx_on[i, k] and np.isfinite(D.o[i, k]) and np.isfinite(D.bo[i, k])):
                    continue
                held = {p['i'] for p in openp}
                if i not in held and len(held) >= K:
                    continue
                px, bpx = D.o[i, k], D.bo[i, k]
                if k >= 2 and D.vol_n[i, k - 2] >= 12:
                    scale = min(max(sigma_ref / max(D.vol_d[i, k - 2], 1e-9), 0.25), 1.0)
                else:
                    scale = 0.5
                gross = sum(abs(p['q']) * p['last'] for p in openp)
                notional = min(E * L * scale / K / len(entries), max(E * L - gross, 0.0))
                if notional <= 0:
                    continue
                sib = [p['stop'] for p in openp if p['i'] == i]
                st = sib[0] if sib else px * (1 + stop)
                c0 = notional * fc + notional * fb
                E -= c0
                taken.add((i, kk))
                openp.append(dict(i=i, k=kk, q=-notional / px, bq=notional / bpx, e_px=px, last=px, blast=bpx, stop=st,
                                  due=int(D.g0[i]) + exit_h + (0 if lag is None else int(lag[i])), pnl=-c0, notional=notional, t_in=t))
        # stops (one per contract)
        fills = {}
        for p in openp:
            k = t - D.g0[p['i']]
            h_ = D.h[p['i'], k]
            if np.isfinite(h_) and h_ >= p['stop']:
                fills[p['i']] = max(fills.get(p['i'], 0.0), max(D.o[p['i'], k], p['stop']))
        if fills:
            keep = []
            for p in openp:
                if p['i'] in fills:
                    k = t - D.g0[p['i']]
                    bpx = D.bc[p['i'], k] if np.isfinite(D.bc[p['i'], k]) else p['blast']
                    close(p, fills[p['i']], bpx, t, 'stop')
                else:
                    keep.append(p)
            openp = keep
        # marks and funding
        for p in openp:
            k = t - D.g0[p['i']]
            c_ = D.c[p['i'], k]
            if np.isfinite(c_):
                d = p['q'] * (c_ - p['last']) - p['q'] * c_ * D.fund[p['i'], k]
                E += d; p['pnl'] += d; p['last'] = c_
            bc_ = D.bc[p['i'], k]
            if np.isfinite(bc_):
                d = p['bq'] * (bc_ - p['blast'])
                if btc_fund is not None:
                    d -= p['bq'] * bc_ * btc_fund[t]
                E += d; p['pnl'] += d; p['blast'] = bc_
        eq[t - gs] = E
    for p in openp:
        done.append(dict(sym=syms[p['i']], i=p['i'], tranche=p['k'], entry_t=p['t_in'], exit_t=ge, ret=-(p['last'] / p['e_px'] - 1),
                         notional=p['notional'], pnl=p['pnl'], nav=E, reason='open_at_end'))
    e = pd.Series(eq, pd.date_range(start, periods=ge - gs, freq='h')).ffill().fillna(1.0)
    daily = e.resample('D').last()
    ret = daily.pct_change().fillna(daily.iloc[0] - 1.0)
    return dict(ret=ret, trades=pd.DataFrame(done), maxdd=float((1 - e / e.cummax()).max()), final=float(e.iloc[-1]),
                killed_at=None if killed_at is None else str(pd.Timestamp(G0, unit='ms') + pd.Timedelta(hours=killed_at)))
