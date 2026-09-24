import sys, numpy as np, pandas as pd
from common import load, minute_of, NMIN, T0
sym = sys.argv[1]; th = float(sys.argv[2]) * 1e-4
X = load(sym)
b = X['b_c'].copy()
ok = X['s_ok'] & X['f_ok'] & np.isfinite(b)
b[~ok] = np.nan
med = pd.Series(b).shift(1).rolling(1440, min_periods=720).median().values
pmed = pd.Series(X['p_c']).shift(1).rolling(1440, min_periods=720).median().values
d = b - med
i0 = minute_of('2022-01-01')
sig = np.where(np.abs(np.nan_to_num(d[i0:])) > th)[0] + i0
ep = sig[np.r_[True, np.diff(sig) > 30]]
rows = []
for e in ep:
    side = np.sign(d[e])
    rows.append(dict(t=T0 + pd.Timedelta(minutes=int(e)), side=int(side), dev=d[e]*1e4, b=b[e]*1e4, med=med[e]*1e4,
                     pdev=(X['p_c'][e]-pmed[e])*1e4, mpc=X['mp_c'][e]*1e4, b_o1=X['b_o'][e+1]*1e4,
                     g1=side*(X['b_o'][e+1]-b[e+1])*1e4, g15=side*(X['b_o'][e+1]-b[e+15])*1e4, g60=side*(X['b_o'][e+1]-b[e+60])*1e4,
                     g60d=side*(b[e+1]-b[e+60])*1e4, sret=(X['s_c'][e]/X['s_c'][e-1]-1)*1e4, srng=(X['s_h'][e]-X['s_l'][e])*1e4))
df = pd.DataFrame(rows)
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 500)
print(df.round(1).to_string())
