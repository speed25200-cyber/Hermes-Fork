"""Independent P&L check of the simulator for the selected config: per trade, recompute from the raw panel the coin-leg
return (entry open -> exit fill), BTC-hedge return, OKX funding received, and fees; P&L = notional_usd x net return.
The sum over trades must match the simulator's equity change (small residual only from the compounding of marks)."""
from sim import *
Dt = Data('hybrid')
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
for start, end in ((IS_START, OOS_START), (OOS_START, OOS_END)):
    r = simulate(Dt, cfg, start, end, record=True)
    tot = 0.0
    for t in r['trades']:
        i = t['i']; g0 = Dt.g0[i]; a = t['entry_t'] - g0; b = t['exit_t'] - g0
        px0, px1 = Dt.o[i, a], t['exit_px']
        q = -t['notional'] / px0
        coin = q * (px1 - px0)
        bq = t['notional'] / Dt.bo[i, a]
        bex = Dt.bc[i, b] if t['reason'] == 'stop' else (Dt.bo[i, b] if b < Dt.H and np.isfinite(Dt.bo[i, b]) else Dt.bc[i, b - 1])
        if t['reason'] == 'open_at_end':
            bex = Dt.bc[i, b - 1]
        hedge = bq * (bex - Dt.bo[i, a])
        last = b if t['reason'] == 'stop' else b - 1
        fund = -np.nansum(q * Dt.c[i, a:last] * Dt.fund[i, a:last])
        fees = (abs(q) * (px0 + px1) * (FEE + SLIP_COIN) + bq * (Dt.bo[i, a] + bex) * (FEE + SLIP_BTC)) if t['reason'] != 'open_at_end' else abs(q) * px0 * (FEE + SLIP_COIN) + bq * Dt.bo[i, a] * (FEE + SLIP_BTC)
        tot += coin + hedge + fund - fees
    print(start, 'sim equity change %.4f | independent sum of trade P&L %.4f | trades %d' % (r['eq'].iloc[-1] - 1, tot, len(r['trades'])))
