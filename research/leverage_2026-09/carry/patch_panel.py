import glob, os, zipfile, io, numpy as np, pandas as pd
D = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data'
P = {k: pd.read_parquet(f'{D}/panel/{k}.parquet') for k in ['f_o','f_h','f_l','f_c','p_h','p_l','p_c']}
def rd(f):
    raw = zipfile.ZipFile(f).read(zipfile.ZipFile(f).namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None if raw[:1].isdigit() else 0).iloc[:, :5]
    df.columns = ['t','o','h','l','c']; df['t'] = pd.to_datetime(df.t.astype('int64'), unit='ms', utc=True)
    return df.set_index('t').astype(float)
n = 0
for f in glob.glob(D + '/daily/*.zip'):
    kind, sym = os.path.basename(f).split('_')[:2]
    df = rd(f)
    if kind == 'fut':
        for c in 'ohlc':
            P['f_'+c].loc[df.index, sym] = P['f_'+c].loc[df.index, sym].fillna(df[c]); n += 1
    else:
        for c in 'hlc':
            P['p_'+c].loc[df.index, sym] = P['p_'+c].loc[df.index, sym].fillna(df[c])
for k, v in P.items():
    v.to_parquet(f'{D}/panel/{k}.parquet'); print(k, int(v.isna().sum().sum()))
