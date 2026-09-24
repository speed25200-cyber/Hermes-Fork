"""Shared loader for the compact 1m panels written by build_data.py."""
import numpy as np, pandas as pd, io, zipfile, glob

D = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev/data'
FUND_DIR = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data'
T0 = pd.Timestamp('2021-12-01', tz='UTC')
NMIN = int((pd.Timestamp('2026-09-01', tz='UTC') - T0).total_seconds() // 60)
SCALE01 = ['b_c', 'b_o', 's_o', 'mp_c', 'p_c', 'i_c']            # 0.1 bp units
SCALE1 = ['s_h', 's_l', 'f_h', 'f_l', 'p_h', 'p_l', 'm_h', 'm_l']  # 1 bp units


def load(sym):
    df = pd.read_parquet(f'{D}/{sym}.parquet')
    out = {}
    for c in SCALE01:
        v = df[c].values
        out[c] = np.where(v == -32768, np.nan, v * 1e-5)
    for c in SCALE1:
        v = df[c].values
        out[c] = np.where(v == -32768, np.nan, v * 1e-4)
    ls = df['ls_c'].values
    out['s_c'] = np.where(ls == -2**31, np.nan, np.exp(ls * 1e-5))
    out['s_ok'] = df['s_ok'].values
    out['f_ok'] = df['f_ok'].values
    return out


def minute_of(ts):
    return int((pd.Timestamp(ts, tz='UTC') - T0).total_seconds() // 60)


def funding(sym):
    """Binance funding events -> array over minutes (rate at the settlement minute, 0 elsewhere)."""
    fs = sorted(glob.glob(f'{FUND_DIR}/data__futures__um__monthly__fundingRate__{sym}__*.zip'))
    rows = []
    for f in fs:
        z = zipfile.ZipFile(f)
        d = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
        rows.append(d)
    d = pd.concat(rows)
    t = d['calc_time'].astype('int64').values
    r = d['last_funding_rate'].astype(float).values
    i = ((t - T0.value // 1_000_000) // 60000).astype(np.int64)
    ok = (i >= 0) & (i < NMIN)
    arr = np.zeros(NMIN)
    np.add.at(arr, i[ok], r[ok])
    return arr, len(fs)
