# Build sim3 = sim2 (pairs_v2.py, unchanged logic) + parameters: tick (maker trade-through), liqmode
# (0 = original wipe-out, 1 = lenient: cross account keeps worst-case equity minus 0.05% of notional and continues).
src = open('pairs_v2.py').read()
a = src.index('@njit(cache=True)\ndef sim2(')
b = src.index('def w_arrays(')
s = src[a:b]
s = s.replace('def sim2(', 'def sim3(')
s = s.replace('maker, use_w, rec):', 'maker, use_w, rec, tick, liqmode):')
s = s.replace('TICK', 'tick')
old = """            ew_c += upl_w; mm_c += mm
"""
new = """            ew_c += upl_w; mm_c += mm; gn_c += abs(qA[k]) * ha + abs(qB[k]) * hb
"""
assert old in s; s = s.replace(old, new)
old = "        ew_c = cash[0]; mm_c = 0.0\n"
assert old in s; s = s.replace(old, "        ew_c = cash[0]; mm_c = 0.0; gn_c = 0.0\n")
old = """                dead = True
                cash[0] = 0.0
                for k in range(K):
                    pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1
                ew_c = 0.0
"""
new = """                if liqmode == 1:
                    resid = ew_c - fee * gn_c
                    if resid < 0.0:
                        resid = 0.0
                    cash[0] = resid
                    if resid <= 0.0:
                        dead = True
                    for k in range(K):
                        pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1; blocked[k] = 1
                    ew_c = resid
                else:
                    dead = True
                    cash[0] = 0.0
                    for k in range(K):
                        pos[k] = 0; qA[k] = 0.0; qB[k] = 0.0; pend[k] = 0; ia[k] = -1; ib[k] = -1
                    ew_c = 0.0
"""
assert old in s; s = s.replace(old, new)
hdr = "import numpy as np\nfrom numba import njit\n\n\n@njit(cache=True)\ndef _close_leg_px(q, px, sl):\n    return px * (1.0 - sl) if q > 0 else px * (1.0 + sl)\n\n\n"
open('sim3.py', 'w').write(hdr + s)
print('ok')
