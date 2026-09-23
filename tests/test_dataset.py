import numpy as np
import pandas as pd

from hermes.config import load_config
from hermes.data.synthetic import make_synthetic_panel
from hermes.research.dataset import build_dataset


def test_chunked_dataset_matches_direct(cfg_small):
    panel = make_synthetic_panel(n_assets=10, n_bars=96 * 80, bar="15m", seed=31)
    direct = build_dataset(panel, cfg_small, chunk_bars=10**9)
    chunked = build_dataset(panel, cfg_small, chunk_bars=96 * 10)
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


def test_chunked_style_targets_match_direct_at_chunk_starts():
    # Short feature windows make the chunk warm-up shorter than the style target's 14-day volume window: the
    # warm-up must be extended, or the first days of every chunk get a different size exposure.
    cfg = load_config(
        None,
        **{
            "data.bar": "15m",
            "data.universe.top_n": 10,
            "data.universe.min_history_days": 3,
            "labels.residualize": "style",
            "features.return_minutes": [15, 60, 240, 1440, 4320],
            "features.range_minutes": [240, 1440, 4320],
            "features.trend_pairs_minutes": [[60, 240], [240, 1440]],
            "features.long_minutes": 4320,
            "features.zscore_minutes": 4320,
            "features.vol_halflife_minutes": 240,
            "features.max_lookback_minutes": 4320,
        },
    )
    panel = make_synthetic_panel(n_assets=10, n_bars=96 * 80, bar="15m", seed=31)
    direct = build_dataset(panel, cfg, chunk_bars=10**9)
    chunked = build_dataset(panel, cfg, chunk_bars=96 * 20)
    assert (direct.t_pos == chunked.t_pos).all() and (direct.s_pos == chunked.s_pos).all()
    ok = np.isfinite(direct.y_raw) & np.isfinite(chunked.y_raw)
    assert ok.mean() > 0.5
    assert np.abs(direct.y_raw[ok] - chunked.y_raw[ok]).max() < 1e-4
