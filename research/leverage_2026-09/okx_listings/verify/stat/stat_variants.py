"""Variant definitions (ex-ante unless noted), applied to any merged/resampled namespace."""
import numpy as np


def kw(m, v):
    okx = (m.ev.src.values == 'okx')
    n = m.n
    if v == 'binance_only':
        return dict(keep=~okx)
    if v == 'union':
        return dict()
    if v == 'okx_half':                      # OKX-extra events at half notional (still occupy a slot)
        return dict(w=np.where(okx, 0.5, 1.0))
    if v == 'okx_nobn':                      # enter an OKX-event tranche only if Binance has no perp at that hour
        return dict(entry_before=np.where(okx, m.bn_g, np.inf))
    if v == 'okx_nobn_half':                 # combination of the two ex-ante variants (reported, not selected)
        return dict(w=np.where(okx, 0.5, 1.0), entry_before=np.where(okx, m.bn_g, np.inf))
    if v == 'okx_exit_bn24':                 # NOT ex-ante: exit (and no entry) from Binance t0 - 24h: upper bound
        x = np.where(okx, m.bn_g - 24, np.inf)
        return dict(entry_before=x, force_xt=x)
    raise KeyError(v)


VARIANTS = ['binance_only', 'union', 'okx_half', 'okx_nobn', 'okx_nobn_half', 'okx_exit_bn24']
