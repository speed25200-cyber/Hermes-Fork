"""End-to-end on the synthetic market: the pipeline must find a planted signal and reject pure noise."""

import numpy as np
import pytest

from hermes.data.synthetic import make_synthetic_panel
from hermes.models.bundle import ModelBundle
from hermes.research.dataset import build_dataset
from hermes.research.run import train_final
from hermes.research.walkforward import walk_forward_train
from hermes.validation.metrics import cross_sectional_ic, ic_summary


@pytest.mark.slow
def test_planted_signal_is_found_out_of_sample(cfg_small):
    panel = make_synthetic_panel(n_assets=16, n_bars=96 * 75, bar="15m", seed=11, signal_strength=1.0)
    ds = build_dataset(panel, cfg_small)
    wf = walk_forward_train(ds, cfg_small)
    s = ic_summary(cross_sectional_ic(wf.score.stack(), ds.targets.residual[4].stack()), horizon=4)
    assert s["ic_mean"] > 0.03 and s["ic_t"] > 3


@pytest.mark.slow
def test_noise_has_no_out_of_sample_ic(cfg_small):
    panel = make_synthetic_panel(n_assets=16, n_bars=96 * 75, bar="15m", seed=12, signal_strength=0.0)
    ds = build_dataset(panel, cfg_small)
    wf = walk_forward_train(ds, cfg_small)
    s = ic_summary(cross_sectional_ic(wf.score.stack(), ds.targets.residual[4].stack()), horizon=4)
    assert abs(s["ic_t"]) < 3
    # The sizing prior is a lower confidence bound: on noise it is zero in most folds.
    priors = [f["prior_ic"] for f in wf.folds]
    assert np.median(priors) == 0.0


@pytest.mark.slow
def test_bundle_roundtrip(cfg_small, tmp_path):
    panel = make_synthetic_panel(n_assets=12, n_bars=96 * 45, bar="15m", seed=13)
    ds = build_dataset(panel, cfg_small)
    b = train_final(ds, cfg_small, promoted=False, evaluation={})
    b.save(tmp_path / "m")
    b2 = ModelBundle.load(tmp_path / "m")
    rows = np.arange(len(ds.t_pos))[-500:]
    s1 = b.score(ds.X[rows], ds.t_pos[rows])
    s2 = b2.score(ds.X[rows], ds.t_pos[rows])
    np.testing.assert_allclose(s1, s2, rtol=1e-6, atol=1e-6)
    assert not b2.promoted
    # Tampering is detected.
    (tmp_path / "m" / "ridge.npz").write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        ModelBundle.load(tmp_path / "m")
