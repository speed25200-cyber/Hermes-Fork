import sys
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"; HERE = SP + '/xlist/verify/premkt'
exec(open(HERE + '/rerun.py').read().split('out = {}')[0])
for p in (SP + '/xlist/data/sim', HERE + '/sim_okx_reanchor', HERE + '/sim_okx_excl'):
    D = load_okx(p)
    r = simulate_live(D, start='2022-01-01', end='2026-09-25', **KW); tr = pd.DataFrame(r['trades'])
    tr = tr.merge(D.ev[['sym', 'cls']], on='sym', how='left')
    print(p.split('/')[-1], tr[tr.sym.isin(['MET-USDT-SWAP', 'RE-USDT-SWAP'])][['sym', 'tranche', 'ret', 'reason']].round(3).values.tolist())
    for c, g in tr.groupby('cls'):
        print('   ', c, len(g), round(g.ret.mean(), 4), round(tstat(g.ret), 2))
