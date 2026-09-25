"""Decompose OOS P&L of the selected config into coin-leg price, BTC hedge, funding, costs; per-trade stats; by month."""
from sim_v import *
Dt = Data('hybrid')
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
for start, end in ((IS_START, OOS_START), (OOS_START, OOS_END)):
    r = simulate(Dt, cfg, start, end)
    rows = []
    for t in r['trades']:
        i = t['i']; g0 = Dt.g0[i]; a = t['entry_t'] - g0; b = min(t['exit_t'] - g0, Dt.H)
        n = t['notional']; q = -n / t['entry_px']
        coin = q * (t['exit_px'] - t['entry_px'])
        bex = Dt.bc[i, b - 1] if b >= 1 else np.nan
        hedge = (n / Dt.bo[i, a]) * (bex - Dt.bo[i, a])
        fund = -np.nansum(q * Dt.c[i, a:b] * Dt.fund[i, a:b])
        cost = abs(q) * (t['entry_px'] + t['exit_px']) * (FEE + SLIP_COIN) + 2 * n * (FEE + SLIP_BTC)
        rows.append(dict(sym=t['sym'], month=str(pd.Timestamp(G0 + t['entry_t'] * HMS, unit='ms').to_period('M')), notional=n, coin=coin, hedge=hedge, fund=fund, cost=cost, ret=t['ret'], reason=t['reason']))
    X = pd.DataFrame(rows)
    print(start, 'trades', len(X), 'sum coin %.4f hedge %.4f fund %.4f cost %.4f | mean notional %.4f | win %.3f | stops %d' % (X.coin.sum(), X.hedge.sum(), X.fund.sum(), X.cost.sum(), X.notional.mean(), (X.ret > 0).mean(), (X.reason == 'stop').sum()))
    if start == OOS_START:
        X['net'] = X.coin + X.hedge + X.fund - X.cost
        print(X.groupby('month').agg(n=('sym', 'size'), net=('net', 'sum')).round(4).T.to_string())
        X.to_csv('oos_trade_decomp.csv', index=False)
