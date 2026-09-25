"""Info: ticker-collision newtok errors (PROS=Pharos vs Binance Prosper 2022; DATA=DATA Network vs Streamr 2018):
set newtok True and re-run OKX-extra (baseline otherwise)."""
import sys, json
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"; HERE = SP + '/xlist/verify/premkt'
exec(open(HERE + '/rerun.py').read().split('out = {}')[0])
out = {}
Do = load_okx(SP + '/xlist/data/sim')
Do.newtok = Do.newtok.copy(); Do.newtok[np.isin(Do.ev.sym.values, ['PROS-USDT-SWAP', 'DATA-USDT-SWAP'])] = True
okx_stats(Do, 'PROS+DATA newtok', out)
r = simulate_live(Do, start='2026-01-01', end='2026-09-25', **KW); tr = pd.DataFrame(r['trades'])
print(tr[tr.sym.isin(['PROS-USDT-SWAP', 'DATA-USDT-SWAP'])][['sym', 'tranche', 'ret', 'reason']])
json.dump(out, open(HERE + '/rerun3.json', 'w'), indent=1)
