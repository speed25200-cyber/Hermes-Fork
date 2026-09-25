"""Sensitivity: also drop GRAM (Toncoin renamed, not a launch) from both sets, on top of the pre-market exclusion."""
import sys, json
sys.argv = ['x']
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"; HERE = SP + '/xlist/verify/premkt'
src = open(HERE + '/rerun.py').read().split('out = {}')[0]      # function defs only
exec(src)
out = {}
Do = load_okx(HERE + '/sim_okx_excl2'); okx_stats(Do, 'excl+GRAM', out)
sim.D = SP + '/newlisting/data'
Db = sim.Data('hybrid'); Db.sigma_ref = SIG
anc = pd.read_csv(HERE + '/bn_pm_anchor.csv')
Db.newtok = Db.newtok.copy(); Db.newtok[anc.i.values.astype(int)] = False
Db.newtok[np.where(Db.ev.sym.values == 'GRAMUSDT')[0]] = False
union_stats(merge(Db, Do, np.zeros(Do.n, bool)), 'binance_only excl+GRAM', out)
union_stats(merge(Db, Do), 'union excl+GRAM', out)
json.dump(out, open(HERE + '/rerun2.json', 'w'), indent=1)
