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


@pytest.mark.slow
def test_research_run_end_to_end_report_resume_and_compare(cfg_small, tmp_path, monkeypatch):
    """run_research on a small synthetic market: gate, report, ledger, bundle, resume and comparison table."""
    import json

    from hermes.research.report import comparison_table
    from hermes.research.run import run_research

    panel = make_synthetic_panel(n_assets=12, n_bars=96 * 60, bar="15m", seed=14, signal_strength=1.0)
    out, ledger = tmp_path / "run", tmp_path / "trials"
    ev, wf, _ = run_research(cfg_small, out, panel=panel, n_null=4, workers=1, ledger=ledger)
    rep = json.loads((out / "report.json").read_text())
    tests = rep["evaluation"]["tests"]
    for k in ("dsr", "null_pvalue", "n_trials", "sharpe_costx2", "sharpe_lag1", "positive_year_fraction"):
        assert k in tests and np.isfinite(tests[k]), k
    assert 1 / 5 <= tests["null_pvalue"] <= 1.0  # exact permutation p-value with 4 nulls
    assert set(ev.gate) >= {"dsr", "null_pvalue", "pbo", "cost_stress", "latency_stress", "oos_months"}
    assert (out / "REPORT.md").exists() and (out / "model" / "bundle.json").exists()
    nh = rep["evaluation"]["nohalt"]  # diagnostic without drawdown controls, over the whole period
    assert np.isfinite(nh["sharpe"]) and nh["by_year"] and "halted_at" in rep["evaluation"]
    assert "sans arrêt" in (out / "REPORT.md").read_text()
    assert len(list(ledger.glob("*.json"))) == 1
    table = comparison_table([out])
    assert table.count("\n") == 2 and "15m" in table
    # Only evaluation settings change: the saved walk-forward is reused, not retrained.
    marker = (out / "walkforward" / "training_hash").read_text()
    cfg2 = cfg_small.model_copy(update={"costs": cfg_small.costs.model_copy(update={"taker_fee": 0.0006})})

    def no_retraining(*a, **k):
        raise AssertionError("the walk-forward was retrained")

    monkeypatch.setattr("hermes.research.run.walk_forward_train", no_retraining)
    _, wf2, _ = run_research(cfg2, out, panel=panel, n_null=2, workers=1, ledger=ledger, save_model=False)
    assert (out / "walkforward" / "training_hash").read_text() == marker
    np.testing.assert_allclose(wf2.score.to_numpy(), wf.score.to_numpy(), rtol=1e-5, atol=1e-6)


def test_resume_accepts_a_longer_saved_history_only():
    from hermes.research.run import _resumable

    saved = "abc:2022-06-01 00:00:00+00:00:2026-08-31 23:45:00+00:00:289"
    shorter = "abc:2022-06-01 00:00:00+00:00:2026-08-31 22:45:00+00:00:289"
    assert _resumable(saved, shorter)  # truncated: every fold only saw its own past
    assert not _resumable(shorter, saved)  # never extended
    assert not _resumable(saved, shorter.replace(":289", ":288"))
    assert not _resumable(saved, "xyz" + shorter[3:])


@pytest.mark.slow
def test_evaluation_window_gives_the_same_backtest():
    """Restricting the evaluation to the out-of-sample window (+ look-back) changes memory, not results."""
    import pandas as pd

    from hermes.backtest.engine import run_backtest
    from hermes.config import load_config
    from hermes.research.evaluate import evaluation_window, make_signal
    from hermes.research.walkforward import WalkForwardResult

    cfg = load_config(None, **{"data.bar": "1h", "data.universe.top_n": 8, "data.universe.min_history_days": 3})
    panel = make_synthetic_panel(n_assets=10, n_bars=24 * 260, bar="1h", seed=31, signal_strength=1.0)
    ds = build_dataset(panel, cfg)
    rng = np.random.default_rng(0)
    oos = ds.mask.index[24 * 220]
    tgt = ds.targets.primary
    score = (tgt + rng.normal(0, 3, tgt.shape)).where(ds.mask).where(ds.mask.index.to_series() >= oos, axis=0)
    wf = WalkForwardResult(score=score, model_scores={}, prior_ic=pd.Series(0.03, index=ds.mask.index))
    ds2, wf2 = evaluation_window(ds, wf, cfg)
    assert len(ds2.mask.index) < len(ds.mask.index)
    runs = []
    for d, w in ((ds, wf), (ds2, wf2)):
        sig = make_signal(w.score, d, w.prior_ic, cfg)
        runs.append(run_backtest(d.panel, d.mask, d.feats.aux, sig, cfg, start=w.oof_start).returns)
    np.testing.assert_allclose(runs[0].to_numpy(), runs[1].reindex(runs[0].index).to_numpy(), atol=1e-9)
