"""Independent recomputation of the headline numbers + kill rule + BTC hedge funding."""
import json
from v_common import *
import indep
Do = load_okx(); Db = load_bn()
U = merge(Db, Do)
Bt = merge(Db, Do, keep_b=np.zeros(Do.n, bool))
d = pd.read_csv(SP + '/xlist/verify/v2_dup_pairs.csv'); ov = d[(d.dh > 0) & (d.dh < 168)]
drop_bn = np.zeros(Db.n, bool); drop_bn[ov.bn_i.values] = True
Ud = merge(Db, Do, keep_a=~drop_bn)
bf = indep.btc_funding_hourly(SP + '/xlist/verify/btc_funding.parquet', indep.gh('2026-09-26'))
sets = [('okx_alone', Do, [('2022-2026', '2022-01-01', '2026-09-25'), ('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-25'), ('2026', '2026-01-01', '2026-09-25')])]
up = [('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-01'), ('2026 Jan-Aug', '2026-01-01', '2026-09-01'), ('2022-2026', '2022-01-01', '2026-09-01')]
sets += [('binance_only', Bt, up), ('union_author', U, up), ('union_dedup_first_venue', Ud, up)]
out = {}
for name, D, periods in sets:
    for pn, a, b in periods:
        ref, _ = run(D, a, b)
        rows = {}
        for vn, kw in [('base', {}), ('btc_funding', dict(btc_fund=bf)), ('kill25', dict(kill_n=25)), ('kill25_L0.8', dict(kill_n=25, L=0.8))]:
            r = indep.sim(D, a, b, **kw)
            tr = r['trades']
            rows[vn] = dict(sharpe=round(sharpe(r['ret']), 3), trades=len(tr), mean=round(float(tr.ret.mean()), 4) if len(tr) else None,
                            maxdd=round(r['maxdd'], 3), final=round(r['final'], 3), killed_at=r['killed_at'])
        rows['livesim_sharpe'] = round(sharpe(ref['ret']), 3)
        rows['match_livesim'] = bool(np.allclose(indep.sim(D, a, b)['ret'].values, ref['ret'].values, atol=1e-10))
        out[f'{name} | {pn}'] = rows
        print(name, pn, json.dumps(rows), flush=True)
json.dump(out, open(SP + '/xlist/verify/v5_indep.json', 'w'), indent=1)
