import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue')
import numpy as np, pandas as pd
import xvenue_bt as bt
P = bt.load_panel(); coins = P['coins']; time = P['time']
S = pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/results/summary.csv')
S = S[S.family == 'funding_diff_carry']
def cfg(L):
    row = S[S.L_per_venue == L].iloc[0]; c = json.loads(row['config']); c['L'] = float(L); return c
def attrib(params):
    r = bt.run(params, record=True)
    q = r['_q']  # [n,2,C] positions at end of step k
    i0 = int(np.searchsorted(time, pd.Timestamp(params['start'], tz='UTC'))); n = q.shape[0]
    idx = np.arange(i0, i0 + n)
    MK = [P['bmc'], P['omc']]; CL = [P['bc'], P['oc']]; FR = [P['fb'], P['fo']]
    qprev = np.concatenate([np.zeros((1, 2, len(coins))), q[:-1]], axis=0)  # held during bar idx
    fund = np.zeros(len(coins)); pnl = np.zeros(len(coins)); cost = np.zeros(len(coins))
    for v in (0, 1):
        fund += -(qprev[:, v, :] * MK[v][idx] * FR[v][idx]).sum(0)
        dp = CL[v][idx] - CL[v][idx - 1]
        pnl += (qprev[:, v, :] * dp).sum(0)
        dq = np.abs(q[:, v, :] - qprev[:, v, :])
        cost += (dq * CL[v][idx] * (0.0005 + P['slip'])).sum(0)
    notional_h = (np.abs(qprev[:, 0, :]) * CL[0][idx]).sum(0)
    df = pd.DataFrame(dict(coin=coins, funding=fund / 1e5, price_pnl=pnl / 1e5, approx_cost=cost / 1e5, hold_notional_years=notional_h / 8760 / 1e5))
    df['net'] = df.funding + df.price_pnl - df.approx_cost
    df = df[(df.hold_notional_years > 0)].sort_values('net')
    return r, df
if __name__ == '__main__':
    pd.set_option('display.width', 250)
    for L in [5.0, 10.0]:
        for per, se in [('IS', ('2022-01-01', '2025-01-01')), ('OOS', ('2025-01-01', '2026-09-01'))]:
            r, df = attrib(dict(cfg(L), start=se[0], end=se[1]))
            print(f'==== L={L} {per} cagr={r["cagr"]:.4f} final={r["final"]:.4f} funding={r["funding_pct"]:.4f} fees={r["fees_pct"]:.4f} slip={r["slip_pct"]:.4f}')
            print(df.round(4).to_string(index=False)); print('sum', df[['funding','price_pnl','approx_cost','net']].sum().round(4).to_dict())
