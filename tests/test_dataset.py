import numpy as np
import pandas as pd

from hermes.data.synthetic import make_synthetic_panel
from hermes.research.dataset import build_dataset


def test_chunked_dataset_matches_direct(cfg_small):
    panel = make_synthetic_panel(n_assets=10, n_bars=24 * 200, seed=31)
    direct = build_dataset(panel, cfg_small, chunk_bars=10**9)
    chunked = build_dataset(panel, cfg_small, chunk_bars=24 * 30)
    assert direct.feature_names == chunked.feature_names
    key = lambda ds: pd.MultiIndex.from_arrays([ds.t_pos, ds.s_pos])  # noqa: E731
    kd, kc = key(direct), key(chunked)
    assert kd.equals(kc), "same (time, symbol) rows in the same order"
    close = np.isclose(direct.X, chunked.X, rtol=2e-2, atol=2e-2) | (np.isnan(direct.X) & np.isnan(chunked.X))
    assert close.mean() > 0.97
    ok = np.isfinite(direct.y) & np.isfinite(chunked.y)
    assert np.corrcoef(direct.y[ok], chunked.y[ok])[0, 1] > 0.99
    for k in ("ivol", "beta"):
        a, b = direct.feats.aux[k].to_numpy(), chunked.feats.aux[k].to_numpy()
        m = direct.mask.to_numpy()
        assert np.nanmax(np.abs(a[m] - b[m]) / (np.abs(a[m]) + 1e-3)) < 0.1
