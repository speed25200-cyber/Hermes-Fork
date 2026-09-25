"""Contrast: the day-1/3 -> day-7 window vs longer windows (event level, short vs BTC, no stop/costs), per year.
Shows which part is persistent and which part is the 2025 new-token bear market. -> contrast.json"""
import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
Dt = Data('hybrid')
yr = pd.to_datetime(Dt.ev.t0, unit='ms').dt.year.values
out = {}
for d0, d1 in ((24, 168), (72, 168), (168, 720), (72, 720), (720, 2160)):
    rows = []
    for i in range(Dt.n):
        if not (Dt.newtok[i] and Dt.okx_on[i, d0] and np.isfinite(Dt.o[i, d0])):
            continue
        if Dt.g0[i] + d1 > gh('2026-09-01'):
            continue
        pe = Dt.o[i, d1] if d1 < Dt.H and np.isfinite(Dt.o[i, d1]) else np.nan
        if not np.isfinite(pe):
            cc = Dt.c[i, d0:d1]; cc = cc[np.isfinite(cc)]
            if len(cc) == 0:
                continue
            pe = cc[-1]
        b = Dt.bo[i, min(d1, Dt.H - 1)] / Dt.bo[i, d0] - 1
        rows.append(dict(y=yr[i], s=-(pe / Dt.o[i, d0] - 1 - b)))
    F = pd.DataFrame(rows)
    g = F.groupby('y').s.agg(['size', 'mean', 'median', lambda v: v.mean() / v.std(ddof=1) * np.sqrt(len(v))])
    g.columns = ['n', 'mean', 'median', 't']
    out[f'{d0}h->{d1}h'] = g.reset_index().to_dict('records')
    print(f'\nshort vs BTC from +{d0}h to +{d1}h'); print(g.round(3).to_string())
json.dump(out, open('contrast.json', 'w'), indent=1, default=str)
