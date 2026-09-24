import pandas as pd, numpy as np, json, multiprocessing as mp
from load import load
import cb_v as cb
AS = load()
TIERS = {'BTC': dict(cb.COST, mmr_fut=0.0065), 'ETH': dict(cb.COST, mmr_fut=0.02)}
g = pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/calbasis/grid_is_lev.csv')
def job(arg):
    rank, p, L = arg
    out = dict(rank=rank, L=L, params=json.dumps(p))
    for per, (a, b) in {'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}.items():
        sims = [cb.Sim(AS[x], p, L, 'cross', TIERS[x], a, b).run() for x in ['BTC', 'ETH']]
        ec, el = cb.equity_series(sims, [.5, .5], a, b)
        if per == 'OOS':
            m = cb.metrics_from(ec, el); out.update(oos_cagr=round(m['cagr']*100, 2), oos_dd=round(m['maxdd']*100, 1), oos_liq=sum(s.nliq for s in sims))
        else:
            e0 = ec[:'2024-12-31 23:55'].iloc[-1]; e1 = ec.iloc[-1]
            out.update(cont_oos_cagr=round(((e1 / e0) ** (365.25 / 608) - 1) * 100 if e1 > 0 else -100, 2), full_liq=sum(s.nliq for s in sims))
    return out
if __name__ == '__main__':
    args = []
    for L in [10, 15, 20]:
        d = g[(g.fam == 'short') & (g.L == L) & (g.nentry + g.nroll >= 6)].sort_values('cagr', ascending=False).drop_duplicates('cagr').head(10)
        args += [(r + 1, json.loads(pj), L) for r, pj in enumerate(d.params)]
    with mp.Pool(4) as pool:
        res = pool.map(job, args)
    df = pd.DataFrame(res); pd.set_option('display.width', 250); print(df.to_string())
    print(df.groupby('L').agg(med_oos=('oos_cagr', 'median'), med_cont=('cont_oos_cagr', 'median'), n_with_liq=('full_liq', lambda x: int((x > 0).sum())),
                              share_cont_pos=('cont_oos_cagr', lambda x: (x > 0).mean())))
    df.to_csv('neighbours_okx_tiers.csv', index=False)
