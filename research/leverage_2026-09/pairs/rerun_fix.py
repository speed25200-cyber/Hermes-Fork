"""Re-run the (scheme, Wz, period) blocks whose maker runs hit the NaN bug (a one-leg maker take-profit completed
at a missing bar close), with the fixed sim2, and splice the corrected rows into the grid files.
Runs that never hit the bug are unaffected by the fix (the NaN was absorbing: fees became NaN for good)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *
import pairs_15m as p15

P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
periods = {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}
for fname, levs, margins, liqs in [('grid_v2', LEVS, ['cross', 'sleeve'], ['h1', 'm1']),
                                   ('grid_v2_low', [1, 3], ['cross'], ['m1'])]:
    path = os.path.join(OUT, fname + '.csv.gz')
    d = pd.read_csv(path)
    bad = d[d.fees.isna()][['method', 'hedge', 'Wf', 'K', 'Wz', 'period']].drop_duplicates()
    if not len(bad):
        continue
    keys = {(r.method, r.hedge, int(r.Wf)) for r in bad.itertuples()}
    pm = [x for x in all_pair_months({k: sel[k] for k in keys}) if x[0] < IS1]
    bounds = minute_bound.compute_bounds(sorted(pm), list(P['syms']), P['o'].astype(np.float64), GRID, FORM_DATES,
                                         log=lambda s: None) if 'm1' in liqs else None
    new = []
    for r in bad.itertuples():
        sig = [s for s in SIG if s[0] == r.Wz]
        x = run_grid2(P, U, sel, bounds, mmr, imr, [(r.method, r.hedge, int(r.Wf), int(r.K))], sig, levs, margins,
                      {r.period: periods[r.period]}, ['maker'], liqs, 'fix', log=lambda s: None)
        if 'm1' in liqs and 'h1' in liqs:
            pass
        new.append(x)
        m = ((d.method == r.method) & (d.hedge == r.hedge) & (d.Wf == r.Wf) & (d.K == r.K) & (d.Wz == r.Wz) &
             (d.period == r.period) & (d.exec == 'maker'))
        print(fname, r.method, r.hedge, r.Wf, r.K, r.Wz, r.period, 'replaced', int(m.sum()), 'with', len(x), flush=True)
        d = d[~m]
    new = pd.concat(new, ignore_index=True)
    assert new.fees.notna().all() and (new.nan_flag == 0).all()
    d = pd.concat([d, new.drop(columns=['nan_flag'])], ignore_index=True)
    assert d.fees.notna().all()
    d.to_csv(path, index=False, compression='gzip')
    print(fname, 'rows', len(d))
