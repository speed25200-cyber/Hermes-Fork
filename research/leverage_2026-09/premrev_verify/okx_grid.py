"""OKX-native 1m test of the premium-reversion rule on EVERY OOS minute (2025-01-01..2026-08-31), BTC and ETH,
using the researcher's engine (engine.gen_trades) fed with OKX data from okxdata/*.parquet:
  basis b = OKX SWAP last / OKX spot last - 1 (1m closes and opens), trailing median over W (past only).
  No OKX premium-index history -> confirm = 0 only. Funding ignored (holds are minutes to hours; funding
  was <= +-2 bp per trade in the Binance runs). Costs: OKX VIP0 taker (perp 5 bp, spot 10 bp), slippage per leg
  1 bp + kappa x that leg's 1m high-low range (kappa 0.05), as in the researcher's runs.
  Margin: 'pm' and 'mc' with the researcher's parameters; the 'mark' is proxied by the OKX last-price basis
  (conservative vs a smoothed mark) and index = spot. Only rows for pm/mc are meaningful ('sep' is not used).
Two uses:
 (1) the Binance-IS-selected rule (btceth W1440 k80 x0 H480 both1 conf0) applied unchanged to OKX OOS (no tuning);
 (2) the whole OOS grid distribution (W x k x x x H x both, conf 0), no selection.
Output: out/okx_grid_oos.csv, out/okx_trades_sel.csv"""
import itertools, sys
import numpy as np, pandas as pd
import engine as E
import run_grid as R

D = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev_verify/okxdata'
A0 = pd.Timestamp('2024-12-30', tz='UTC')
OOS_A = int((pd.Timestamp('2025-01-01', tz='UTC') - A0).total_seconds() // 60)
PARAMS = {'BTC': R.PARAMS['BTCUSDT'], 'ETH': R.PARAMS['ETHUSDT']}


def prep(coin):
    df = pd.read_parquet(f'{D}/{coin}_2024-12-30_2026-09-01.parquet')
    n = len(df)
    dq = lambda c, s: np.where(df[c].values == -32768, np.nan, df[c].values * s)
    ok = df.s_ok.values & df.f_ok.values & np.isfinite(dq('b_c', 1e-5))
    bsig = np.where(ok, dq('b_c', 1e-5), np.nan)
    ff = lambda a: pd.Series(a).ffill().bfill().values
    bc = ff(bsig)
    Sc = ff(df.sc.values.astype(float))
    z = lambda a: np.nan_to_num(a, nan=0.0)
    P = dict(ok=ok, bc=bc, Sc=Sc, so=z(dq('s_o', 1e-5)), bo=np.where(np.isfinite(dq('b_o', 1e-5)), dq('b_o', 1e-5), bc),
             sh=z(dq('s_rng', 1e-4)), sl=np.zeros(n), fh=z(dq('f_rng', 1e-4)), fl=np.zeros(n),
             mpc=bc.copy(), ph=np.zeros(n), pl=np.zeros(n), mh=bc.copy(), ml=bc.copy(), ic=np.zeros(n),
             fund=np.zeros(n))
    r = pd.read_csv(R.USDT_RATE, parse_dates=['ts']).drop_duplicates('ts').set_index('ts')['rate']
    idx = A0 + pd.to_timedelta(np.arange(0, n, 60), unit='min')
    P['rusdt'] = np.repeat(r.reindex(idx, method='ffill').bfill().values, 60)[:n]
    P['med'] = {W: pd.Series(bsig).shift(1).rolling(W, min_periods=W // 2).median().values for W in R.GRID['W']}
    P['n'] = n
    print(coin, 'minutes', n, 'ok %.4f' % ok.mean(), 'basis median %.1f bp' % (np.nanmedian(bsig) * 1e4), flush=True)
    return P


def trades(P, coin, cfg, lat, kappa=0.05):
    pr = PARAMS[coin]
    W, k, x, H, both = cfg
    return E.gen_trades(OOS_A, P['n'] - 10, P['ok'], P['bc'], P['bo'], P['Sc'], P['so'], P['sh'], P['sl'], P['fh'], P['fl'],
                        P['mpc'], P['ph'], P['pl'], P['mh'], P['ml'], P['ic'], P['fund'], P['rusdt'],
                        P['med'][W], np.zeros(P['n']), k * 1e-4, x, H, both, 0, 0, lat, R.T_FILL, R.BUF,
                        R.FEES['fs_t'], R.FEES['fp_t'], R.FEES['fs_m'], R.FEES['fp_m'], pr['slip'], kappa,
                        pr['haircut'], pr['mm'], pr['im'], pr['lp'], pr['bmmr'], pr['cbmmr'], pr['mmr_m'], pr['m_pm'],
                        R.R_COIN, E.LEVS, 0, 1)


def main():
    Ps = {c: prep(c) for c in ['BTC', 'ETH']}
    n = min(P['n'] for P in Ps.values())
    b = n - 10
    yrs = (b - OOS_A) / 1440 / 365.25
    rows = []
    cfgs = list(itertools.product(R.GRID['W'], R.GRID['k'] + [100, 120, 150], R.GRID['x'], R.GRID['H'], R.GRID['both']))
    seltr = []
    for lat in [0, 1]:
        for cfg in cfgs:
            trd = {c: trades(Ps[c], c, cfg, lat) for c in Ps}
            for un, uc in {'btceth': ['BTC', 'ETH'], 'btc': ['BTC'], 'eth': ['ETH']}.items():
                allt = R.merge([trd[c] for c in uc])
                if not len(allt):
                    continue
                for m in ['pm', 'mc']:
                    cr, cw, cl, cj = E.col(m, 'ret'), E.col(m, 'worst'), E.col(m, 'liq'), E.col(m, 'rej')
                    met, _ = R.port_sim(allt[:, E.C_SIG].copy(), allt[:, E.C_E].copy(), allt[:, E.C_X].copy(),
                                        np.ascontiguousarray(allt[:, cr:cr + E.NL]), np.ascontiguousarray(allt[:, cw:cw + E.NL]),
                                        np.ascontiguousarray(allt[:, cl:cl + E.NL]), np.ascontiguousarray(allt[:, cj:cj + E.NL]),
                                        OOS_A, b, OOS_A // 1440, b // 1440 - OOS_A // 1440)
                    net = allt[:, E.C_GROSS] - allt[:, E.C_FEES] - allt[:, E.C_SLIP] + allt[:, E.C_FUND]
                    for li, L in enumerate(E.LEVS):
                        fin, mdd, nliq, ntk, sh, wd, nrej = met[li]
                        rows.append(dict(lat=lat, univ=un, W=cfg[0], k=cfg[1], x=cfg[2], H=cfg[3], both=cfg[4], margin=m, L=L,
                                         final=fin, cagr=fin ** (1 / yrs) - 1 if fin > 0 else -1.0, maxdd=mdd, liqs=int(nliq),
                                         trades=int(ntk), rejected=int(nrej), sharpe=sh, n_all=len(allt),
                                         edge_bp=net.mean() * 1e4, gross_bp=allt[:, E.C_GROSS].mean() * 1e4))
                if un == 'btceth' and cfg == (1440, 80, 0.0, 480, 1):
                    for r in allt:
                        seltr.append(dict(lat=lat, t=A0 + pd.Timedelta(minutes=int(r[0])), side=int(r[3]), dev_bp=r[12] * 1e4,
                                          b0_bp=r[11] * 1e4, hold=int(r[2] - r[1]), gross_bp=r[4] * 1e4, fees_bp=r[5] * 1e4,
                                          slip_bp=r[6] * 1e4, net_bp=(r[4] - r[5] - r[6] + r[7]) * 1e4))
    df = pd.DataFrame(rows)
    df.to_csv(f'{R.OUTDIR}/okx_grid_oos.csv', index=False, float_format='%.6g')
    st = pd.DataFrame(seltr); st.to_csv(f'{R.OUTDIR}/okx_trades_sel.csv', index=False, float_format='%.5g')
    pd.set_option('display.width', 250); pd.set_option('display.max_rows', 300)
    print('OOS years', round(yrs, 3))
    print('--- Binance-IS-selected rule on OKX OOS (btceth W1440 k80 x0 H480 both, conf0) ---')
    s = df[(df.univ == 'btceth') & (df.W == 1440) & (df.k == 80) & (df.x == 0) & (df.H == 480) & (df.both == 1)]
    print(s[['lat', 'margin', 'L', 'cagr', 'maxdd', 'liqs', 'trades', 'rejected', 'edge_bp', 'n_all']].round(4).to_string(index=False))
    print(st.round(1).to_string(index=False))
    print('--- whole OOS grid distribution (btceth, pm) ---')
    g = df[(df.univ == 'btceth') & (df.margin == 'pm')]
    print(g.groupby(['lat', 'L']).apply(lambda h: pd.Series(dict(n=len(h), n_pos=int((h.cagr > 0).sum()), med=h.cagr.median(),
                                                                 q90=h.cagr.quantile(.9), mx=h.cagr.max(), n_liq=int((h.liqs > 0).sum())))).round(4).to_string())
    e1 = df[(df.univ == 'btceth') & (df.margin == 'pm') & (df.L == 1)]
    print(e1.groupby(['lat', 'k']).agg(n=('edge_bp', 'size'), edge_med=('edge_bp', 'median'), edge_max=('edge_bp', 'max'),
                                       n_pos=('edge_bp', lambda v: int((v > 0).sum())), trades_med=('n_all', 'median')).round(1).to_string())


if __name__ == '__main__':
    main()
