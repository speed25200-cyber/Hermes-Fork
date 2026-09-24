import sys, numpy as np, pandas as pd
import run_grid as R, engine as E
from common import T0
sym = sys.argv[1]; cfg = tuple(eval(sys.argv[2])); mode = int(sys.argv[3]); lat = int(sys.argv[4])
P = R.prep(sym)
tr = R.trades_for(P, sym, cfg, mode, lat)
df = pd.DataFrame(tr[:, :13], columns=['sig','e','x','side','gross','fees','slip','fund','hours','mkin','mkout','b0','dev'])
df['t'] = T0 + pd.to_timedelta(df.sig, unit='min'); df['hold'] = df.x - df.e
df['net'] = (df.gross - df.fees - df.slip + df.fund) * 1e4
pd.set_option('display.width', 250)
print(df.sort_values('net').head(8)[['t','side','dev','b0','hold','gross','net','mkin','mkout']].to_string())
# inspect the worst one minute by minute
w = df.sort_values('net').iloc[0]
s, x = int(w.sig), int(w.x)
m = pd.DataFrame({k: P[k][s-2:x+3] for k in ['bc','bo','Sc','sh','sl','fh','fl','mpc']})
m['med'] = P['med'][cfg[0]][s-2:x+3]; m['ok'] = P['ok'][s-2:x+3]
m.index = T0 + pd.to_timedelta(np.arange(s-2, x+3), unit='min')
for c in ['bc','bo','sh','sl','fh','fl','mpc','med']: m[c] = (m[c]*1e4).round(1)
print(m.head(12).to_string()); print(m.tail(6).to_string())
