"""Patched copy of newlisting/sim.simulate: fair_lat (stops stay intrabar when lat>0), stop_slip, stop_worst."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'newlisting'))
import sim
# ---- patched simulator: fair latency + stop slippage (copy of sim.simulate with 2 edits)
src = open(os.path.join(HERE, '..', 'newlisting', 'sim.py')).read()
fn = src[src.index('def simulate('):src.index('def sharpe(')]
fn = fn.replace("min_frac=0.0, record=False):", "min_frac=0.0, record=False, stop_slip=0.0, stop_worst=False, fair_lat=False):")
old = "                    if lat == 0:\n                        fill = max(o_, p['stop_px']) if p['q'] < 0 else min(o_, p['stop_px'])"
assert old in fn
new = ("                    if lat == 0 or fair_lat:\n                        fill = max(o_, p['stop_px']) if p['q'] < 0 else min(o_, p['stop_px'])\n"
       "                        fill = min(h_, fill * (1 + stop_slip)) if p['q'] < 0 else max(l_, fill * (1 - stop_slip))\n"
       "                        if stop_worst: fill = h_ if p['q'] < 0 else l_")
fn = fn.replace(old, new)
ns = dict(sim.__dict__); exec(fn, ns); simulate2 = ns['simulate']
