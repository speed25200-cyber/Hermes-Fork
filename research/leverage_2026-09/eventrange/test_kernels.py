"""Hand-checked scenarios for the three kernels (synthetic 1m paths)."""
import numpy as np
from kernels import wick_sim, grid_sim, orb_sim
def mk(path_o, path_h, path_l, path_c):
    n = len(path_o)
    o = np.array(path_o, float); h = np.array(path_h, float); l = np.array(path_l, float); c = np.array(path_c, float)
    day = np.zeros(n, np.int64); month = np.zeros(n, np.int64)
    return o, h, l, c, h.copy(), l.copy(), c.copy(), np.zeros(n), np.zeros(n, np.int8), day, month
FT, FM = 0.0005, 0.0002
# --- wick 1: event at bar 1 close (long, mf=2%), market entry at bar 2 open=100, tp 0.5*2% = 1% -> 101 hit at bar 4
o=[100,98,100,100.5,100.8,101,101]; h=[100,100,100.3,100.9,101.2,101,101]; l=[100,98,99.9,100.4,100.7,101,101]; c=[100,98,100.2,100.8,101,101,101]
a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64); trd=np.zeros(10)
n = wick_sim(*a, 0, 7, np.array([1],np.int64), np.array([1],np.int64), np.array([0.02]), 0.1, 0.004, 0.0, 10.0, False, 0.5, 1.0, 100, False, 30, 0.25, 15, FT, FM, 0.0, 0.75, r, tr, st, trd)
exp = 1 - FT*10 + 10*(101/100-1) - FM*10*1.01
print('wick tp', n, st[:5], trd[:n], 'expected', exp-1, 'got', r[0])
assert abs(r[0] - (exp-1)) < 1e-9
# --- wick 2: stop at 1*2% = 98, and at L=20 liq ~ 95.5; bar 3 low 95 -> both hit -> liquidation
o=[100,98,100,99,99]; h=[100,100,100.1,99,99]; l=[100,98,99.5,95,99]; c=[100,98,99.8,96,99]
a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64)
n = wick_sim(*a, 0, 5, np.array([1],np.int64), np.array([1],np.int64), np.array([0.02]), 0.1, 0.004, 0.0, 20.0, False, 0.5, 1.0, 100, False, 30, 0.25, 15, FT, FM, 0.0, 0.75, r, tr, st, trd)
print('wick liq', st[:5], r[0]); assert st[1] == 1 and r[0] == -1
# same but low 97.5 -> stop only, loss = 20*(98/100-1) - fees
l=[100,98,99.5,97.5,99]; a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64)
n = wick_sim(*a, 0, 5, np.array([1],np.int64), np.array([1],np.int64), np.array([0.02]), 0.1, 0.004, 0.0, 20.0, False, 0.5, 1.0, 100, False, 30, 0.25, 15, FT, FM, 0.0, 0.75, r, tr, st, trd)
exp = 1 - FT*20 + 20*(98/100-1) - FT*20*0.98
print('wick stop', st[:5], r[0], exp-1); assert abs(r[0]-(exp-1))<1e-9
# --- wick 3: limit entry at c[1]*(1-0.25*0.02) = 98*0.995 = 97.51; bar 2 low 97.5 not through by tick 0.1 -> no fill; bar 3 low 97.3 -> fill
o=[100,98,98,97.6,98,98.6,99]; h=[100,100,98.2,97.8,98.5,99,99]; l=[100,98,97.5,97.3,97.9,98.5,99]; c=[100,98,97.7,97.8,98.4,98.9,99]
a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64)
n = wick_sim(*a, 0, 7, np.array([1],np.int64), np.array([1],np.int64), np.array([0.02]), 0.1, 0.004, 0.0, 5.0, True, 0.5, 1.0, 100, False, 30, 0.25, 15, FT, FM, 0.0, 0.75, r, tr, st, trd)
P0 = 98*0.995; tp = P0*(1+0.01)
print('wick lim', st[:7], r[0], 'tp', tp)   # tp=98.49 -> bar 5 high 99 >= tp+0.1 -> tp fill
exp = 1 - FM*5 + 5*(tp/P0-1) - FM*5*tp/P0
assert st[6] == 1 and st[3] == 1 and abs(r[0]-(exp-1))<1e-9
# --- grid: fixed anchor 100, g=1%, N=2, L=2 -> unit = 2*1/(2*100) = 0.01 coins. price 100 -> 98.9 (buy @99) -> 100.2 (sell @100): +1 coin-unit*1 - fees
o=[100,100,99.5,99.8]; h=[100,100,99.9,100.2]; l=[100,99.5,98.9,99.7]; c=[100,99.6,99.8,100.1]
a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64)
grid_sim(*a, 0, 4, 0.1, 0.004, 0.0, 2.0, 0.01, 2, 0.0, True, FT, FM, 0.0, r, tr, st)
u = 0.01; exp = 1 - 99*u*(1+FM) + 100*u*(1-FM)
print('grid rt', st[:5], r[0], exp-1); assert abs(r[0]-(exp-1))<1e-12
# grid trend down with stop: 100 -> 96.8 in one bar: buys at 99, 98, stop at 97 (N+1=3) -> close 2 units at 97
o=[100,100,97]; h=[100,100,97]; l=[100,96.8,97]; c=[100,97,97]
a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64)
grid_sim(*a, 0, 3, 0.1, 0.004, 0.0, 2.0, 0.01, 2, 0.0, True, FT, FM, 0.0, r, tr, st)
exp = 1 - (99+98)*u*(1+FM) + 2*u*97*(1-FT)
print('grid stop', st[:5], r[0], exp-1); assert abs(r[0]-(exp-1))<1e-12
# grid hold at L=20, N=2, g=1%: unit = 20/(2*100)=0.1 coin; long 2 units avg 98.5 (notional ~19.7); liq when 1-0.1*(99-p)-0.1*(98-p) - fees... p ~ 93.9
o=[100,100,97,95,94]; h=[100,100,97,95,94]; l=[100,97.5,95,93.5,94]; c=[100,97.6,95.2,94,94]
a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64)
grid_sim(*a, 0, 5, 0.1, 0.004, 0.0, 20.0, 0.01, 2, 0.0, False, FT, FM, 0.0, r, tr, st)
print('grid hold liq', st[:5], r[0]); assert st[1] == 1 and r[0] == -1
# --- ORB: arm at bar 2 with orh=101, orl=99; bar 3 breaks up (entry 101.1), tp 1x range -> 103.1 at bar 5
o=[100,100.5,100,100.9,102,103]; h=[101,100.8,100.5,101.5,102.8,103.5]; l=[99,100,99.8,100.8,101.8,102.9]; c=[100.5,100,100.4,101.3,102.7,103.3]
a = mk(o,h,l,c); r=np.zeros(1); tr=np.ones(1); st=np.zeros(8,np.int64)
n = orb_sim(*a, 0, 6, np.array([2],np.int64), np.array([6],np.int64), np.array([100],np.int64), np.array([101.0]), np.array([99.0]), np.array([1],np.int64), 0.1, 0.004, 0.0, 3.0, False, 1.0, FT, FM, 0.0, 0.75, r, tr, st, trd)
P0 = 101.1; tp = P0 + 2.0; exp = 1 - FT*3 + 3*(tp/P0-1) - FM*3*tp/P0
print('orb', st[:5], r[0], exp-1); assert abs(r[0]-(exp-1))<1e-9 and st[3]==1
print('ALL OK')
