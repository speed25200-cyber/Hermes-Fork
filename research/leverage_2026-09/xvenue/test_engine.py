"""Synthetic sanity tests for xvenue_bt (accounting, funding, liquidation, de-risk)."""
import numpy as np, pandas as pd
import xvenue_bt as bt

def fake(n=24*60, price=None, hi=None, lo=None, fo=0.0001, fb=0.0):
    time = pd.date_range('2022-01-01 01:00', periods=n, freq='h', tz='UTC')
    pr = np.full((n,1), 100.0) if price is None else price.reshape(-1,1)
    H = pr.copy() if hi is None else hi.reshape(-1,1)
    Lw = pr.copy() if lo is None else lo.reshape(-1,1)
    F_o = np.zeros((n,1)); F_b = np.zeros((n,1))
    settle = np.array([t.hour % 8 == 0 for t in time])
    F_o[settle,0] = fo; F_b[settle,0] = fb
    P = dict(coins=['BTC'], time=time, bc=pr, oc=pr.copy(), bmc=pr.copy(), omc=pr.copy(), bh=H, bl=Lw, oh=H.copy(), ol=Lw.copy(),
             bmh=H.copy(), bml=Lw.copy(), omh=H.copy(), oml=Lw.copy(), fb=F_b, fo=F_o,
             valid=np.ones((n,1),bool), listed=np.ones((n,1),bool), slip=np.array([1e-4]), mmr=np.array([0.005]))
    return P

base = dict(start='2022-01-01', end='2030-01-01', K=1, th_in=0.0, th_out=0.0, H=24, fee=0.0, slip_mult=0.0, transfer_fee=0.0)
# 1. pure carry, no costs: 60 days, L=1 -> per-leg notional 50k, 0.01% per 8h received on OKX short
P = fake(); bt._CACHE.clear()
r = bt.run(dict(base, L=1.0), P=P)
# signal needs 72h warm-up -> first entry at the first decision hour after 72 bars
print('carry L=1 final', r['final'], 'funding_pct', r['funding_pct'], 'entries', r['entries'])
r = bt.run(dict(base, L=10.0), P=P)
print('carry L=10 final', r['final'], 'funding_pct', r['funding_pct'], 'expected approx', 1+ 5*0.0001*3*(60-3.3))
# 2. with fees: entry cost = 2 fills * 5bp on 50k*L
r = bt.run(dict(base, L=10.0, fee=0.0005), P=P)
print('carry L=10 with fees final', r['final'], 'fees_pct', r['fees_pct'])
# 3. price jumps +9.6% in one bar at bar 500 (both venues), stays. L=10 per venue, mmr 0.5%
pr = np.full(24*60, 100.0); pr[500:] = 109.6
for mode in ['hourly', 'intrabar']:
    P = fake(price=pr); bt._CACHE.clear()
    r = bt.run(dict(base, L=10.0, risk_mode=mode), P=P)
    print(mode, 'jump 9.6%: liq', r['liq'], 'cuts', r['cuts'], 'final', round(r['final'],4), 'maxdd', round(r['maxdd'],4), r['liq_events'][:1])
# 4. spike to +12% intrabar and back to 100 at close
hi = np.full(24*60, 100.0); hi[500] = 112.0
for mode in ['hourly', 'intrabar']:
    P = fake(hi=hi); bt._CACHE.clear()
    r = bt.run(dict(base, L=10.0, risk_mode=mode), P=P)
    print(mode, 'spike 12% revert: liq', r['liq'], 'cuts', r['cuts'], 'final', round(r['final'],4), 'maxdd', round(r['maxdd'],4))
# 5. slow drift +1%/day for 20 days -> transfers should prevent liquidation at L=10, R=4, D=4
pr = 100*np.cumprod(np.r_[np.ones(200), np.full(480, 1.01**(1/24)), np.ones(24*60-680)])
P = fake(price=pr); bt._CACHE.clear()
for L in [10, 20]:
    r = bt.run(dict(base, L=float(L), risk_mode='hourly', fee=0.0005, slip_mult=1.0), P=P)
    print('drift L', L, 'liq', r['liq'], 'transfers', r['transfers'], 'realize', r['realize'], 'dlev', r['dlev'], 'final', round(r['final'],4))
