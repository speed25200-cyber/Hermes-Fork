"""Fixed factor bundles use the same scores in research and offline live decisions."""

import asyncio
import os

import numpy as np
import pandas as pd
import pytest

from hermes.config import with_overrides
from hermes.data.universe import universe_mask
from hermes.execution.broker import PaperBroker
from hermes.features.library import STORAGE_DTYPE, FeatureSet, build_features
from hermes.live.engine import Decision, LiveEngine
from hermes.live.state import StateStore
from hermes.models.bundle import ModelBundle
from hermes.models.factors import build_factor_bundle, score_factor_bundle


def _engine(cfg, bundle, tmp_path, *, mode="paper", model_dir=None):
    return LiveEngine(
        cfg,
        bundle,
        None,
        PaperBroker(tmp_path / "account.json", 10_000, 0.0002, 0.0005),
        StateStore(tmp_path / "state"),
        mode=mode,
        model_dir=model_dir,
    )


def _expected(bundle, panel):
    mask = universe_mask(panel, bundle.config.data.universe)
    feats = build_features(panel, mask, bundle.config.features)
    return score_factor_bundle(bundle, feats, mask.iloc[[-1]]).iloc[-1].dropna()


def test_factor_bundle_paper_lifecycle_scores_match_research(cfg_small, small_panel, tmp_path):
    cfg = with_overrides(cfg_small, {"live.paper_nominal_size": False})
    champion = tmp_path / "champion"
    build_factor_bundle(cfg, "momentum").save(champion)
    bundle = ModelBundle.load(champion)
    assert bundle.feature_names == ["iret_10080m", "iret_1440m"]  # reverses library column order
    eng = _engine(cfg, bundle, tmp_path, model_dir=champion)
    panel = small_panel.iloc(slice(0, 96 * 45))
    decision = eng.decide(panel, {}, 10_000)
    expected = _expected(bundle, panel)
    stored = eng.store.score_history(panel.index[0]).loc[panel.index[-1]].dropna()
    pd.testing.assert_series_equal(stored.sort_index(), expected.sort_index(), check_names=False)
    assert decision.n_members >= 5 and decision.scores
    assert decision.ic_est == 0 and not decision.targets
    assert not bundle.promoted
    eng.broker.set_prices(panel["close"].iloc[-1].dropna().to_dict())
    assert asyncio.run(eng.broker.rebalance(decision.targets)).fills == []
    assert asyncio.run(eng.broker.equity()) == 10_000

    # Saving/reloading another fixed rule must replace its feature subset and coefficients together.
    old_ts = panel.index[-1]
    signal_names = ("ic", "ic_h8", "ic_raw", "ic_lag", "btc_dd90", "mkt_ret30", "xs_ac1", "mkt_score", "mkt_target")
    for name in signal_names:
        eng._record_once(name, pd.Series({old_ts: 0.9}), old_ts)
        assert eng._series(name, old_ts).loc[old_ts] == 0.9
    epoch_before = eng.store.get("factor_signal_epoch")
    build_factor_bundle(cfg, "reversal").save(champion)
    later = (champion / "bundle.json").stat().st_mtime + 10
    os.utime(champion / "bundle.json", (later, later))
    assert eng.maybe_reload_bundle()
    assert eng.bundle.meta["factor_rule"] == "reversal"
    later_panel = small_panel.iloc(slice(0, 96 * 45 + 1))
    later_decision = eng.decide(later_panel, {}, 10_000)
    stored = eng.store.score_history(panel.index[0]).loc[later_panel.index[-1]].dropna()
    pd.testing.assert_series_equal(
        stored.sort_index(), _expected(eng.bundle, later_panel).sort_index(), check_names=False
    )
    assert later_decision.scores and not later_decision.targets and later_decision.ic_est == 0
    assert eng.store.get("factor_signal_epoch")["identity"] != epoch_before["identity"]
    assert eng._memory("scores", later_panel.index[-1]).index.tolist() == [later_panel.index[-1]]
    for name in signal_names:
        assert eng._series(name, later_panel.index[-1]).empty
        assert eng.store.get_series(name, old_ts).loc[old_ts] == 0.9  # retained for audit
    pd.testing.assert_series_equal(
        eng.store.score_history(panel.index[0]).loc[old_ts].dropna().sort_index(),
        expected.sort_index(),
        check_names=False,
    )


@pytest.mark.parametrize("change", ["rule", "config", "coefficients"])
def test_factor_restart_isolates_signals_and_preserves_accounting(cfg_small, tmp_path, change):
    t0, t1, t2 = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
    first = _engine(cfg_small, build_factor_bundle(cfg_small, "reversal"), tmp_path)
    first._prepare_factor_epoch(t0)
    first.store.add_scores(t0, pd.Series({"BTCUSDT": 1.0}))
    first.store.put_series("ic", pd.Series({t0: 0.8}))
    first.overlay.observe(t0, 10_000)
    first._save_risk_state()
    first.store.put("nav_state", {"nav": 10_000, "equity": 10_000, "frac": 1})
    first.store.put("paper_funding_exposures", {"pending": {"id": "unpaid", "cash": 2.0}})
    first.store.put("book_state", {"held": "existing positions"})
    first.broker.set_prices({"BTCUSDT": 100.0})
    asyncio.run(first.broker.rebalance({"BTCUSDT": 1000.0}))
    account_before = (tmp_path / "account.json").read_bytes()
    keys = ("risk_state", "nav_state", "paper_funding_exposures", "book_state")
    state_before = {key: first.store.get(key) for key in keys}
    old_identity = first.store.get("factor_signal_epoch")["identity"]

    cfg = with_overrides(cfg_small, {"portfolio.cost_aversion": 2.0}) if change == "config" else cfg_small
    bundle = build_factor_bundle(cfg, "momentum" if change == "rule" else "reversal")
    if change == "coefficients":
        bundle.ridge.coef[0] *= -1  # identity must track the actual model, even if metadata stayed unchanged
    restarted = _engine(cfg, bundle, tmp_path)
    restarted._prepare_factor_epoch(t1)
    assert restarted.store.get("factor_signal_epoch")["identity"] != old_identity
    assert restarted._memory("scores", t1).empty and restarted._series("ic", t1).empty
    assert (tmp_path / "account.json").read_bytes() == account_before
    assert {key: restarted.store.get(key) for key in keys} == state_before
    assert restarted.store.score_history(t0).loc[t0, "BTCUSDT"] == 1.0
    assert restarted.store.get_series("ic", t0).loc[t0] == 0.8

    # Late writes cannot rewrite a prior epoch or persist future observations through the signal path.
    restarted._record_once("ic", pd.Series({t0: -0.8, t1: 0.2, t2: 0.9}), t1)
    assert restarted.store.get_series("ic", t0).to_dict() == {t0: 0.8, t1: 0.2}
    restarted.store.add_scores(t1, pd.Series({"BTCUSDT": -1.0}))
    restarted.store.add_scores(t2, pd.Series({"BTCUSDT": 99.0}))
    restarted.store.put_series("ic", pd.Series({t2: 99.0}))
    same = _engine(cfg, bundle, tmp_path)
    assert same._memory("scores", t1).index.tolist() == [t1]
    assert same._series("ic", t1).to_dict() == {t1: 0.2}
    with pytest.raises(RuntimeError, match="precedes its signal epoch"):
        same._series("ic", t0)


def test_factor_epoch_cannot_overwrite_prior_or_future_signal_rows(cfg_small, tmp_path):
    t0, t1, t2 = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
    first = _engine(cfg_small, build_factor_bundle(cfg_small, "reversal"), tmp_path)
    first._prepare_factor_epoch(t0)
    first.store.add_scores(t0, pd.Series({"BTCUSDT": 1.0}))
    first.store.put_series("ic_h8", pd.Series({t2: 0.8}))
    previous_epoch = first.store.get("factor_signal_epoch")
    new = _engine(cfg_small, build_factor_bundle(cfg_small, "momentum"), tmp_path)
    for ts in (t0, t1, t2):
        with pytest.raises(RuntimeError, match="requires a bar after"):
            new._prepare_factor_epoch(ts)
        assert new.store.get("factor_signal_epoch") == previous_epoch
    # Even an epoch that produced no scores yet cannot be replaced by a decision from its past.
    empty_state = tmp_path / "empty"
    pending = _engine(cfg_small, build_factor_bundle(cfg_small, "reversal"), empty_state)
    pending._prepare_factor_epoch(t2)
    replacement = _engine(cfg_small, build_factor_bundle(cfg_small, "momentum"), empty_state)
    with pytest.raises(RuntimeError, match="requires a bar after"):
        replacement._prepare_factor_epoch(t1)


def test_factor_return_after_ml_interlude_starts_a_new_epoch(cfg_small, tmp_path):
    t0, t1, t2 = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
    factor = build_factor_bundle(cfg_small, "reversal")
    first = _engine(cfg_small, factor, tmp_path)
    first._prepare_factor_epoch(t0)
    first.store.add_scores(t0, pd.Series({"BTCUSDT": 1.0}))
    first.store.put_series("ic", pd.Series({t0: 0.9}))
    ml = build_factor_bundle(cfg_small, "reversal")
    ml.meta.pop("model_kind")
    middle = _engine(cfg_small, ml, tmp_path)
    middle._prepare_factor_epoch(t1)
    middle.store.add_scores(t1, pd.Series({"BTCUSDT": 99.0}))
    assert middle._memory("scores", t1).index.tolist() == [t1]
    assert middle._series("ic", t1).empty  # an ML model must not inherit a factor's IC either
    returned = _engine(cfg_small, factor, tmp_path)
    returned._prepare_factor_epoch(t2)
    assert returned._memory("scores", t2).empty
    assert len(returned.store.score_history(t0)) == 2


def test_ordinary_ml_signal_history_without_a_factor_epoch_is_unchanged(cfg_small, tmp_path):
    t0, t1 = pd.date_range("2026-01-01", periods=2, freq="15min", tz="UTC")
    bundle = build_factor_bundle(cfg_small, "reversal")
    bundle.meta.pop("model_kind")
    eng = _engine(cfg_small, bundle, tmp_path)
    eng.store.add_scores(t0, pd.Series({"BTCUSDT": 1.0}))
    eng.store.put_series("ic", pd.Series({t0: 0.9}))
    eng._prepare_factor_epoch(t1)
    assert eng.store.get("factor_signal_epoch") is None
    assert eng._memory("scores", t1).index.tolist() == [t0]
    assert eng._series("ic", t1).loc[t0] == 0.9


def test_unpromoted_factor_bundle_cannot_keep_real_risk(cfg_small, small_panel, tmp_path):
    bundle = build_factor_bundle(cfg_small, "reversal")
    eng = _engine(cfg_small, bundle, tmp_path, mode="live")
    panel = small_panel.iloc(slice(0, 96 * 45))
    held = panel.symbols[0]
    decision = eng.decide(panel, {held: 1_000.0}, 10_000.0)
    assert decision.scores
    assert decision.targets[held] == 0.0
    assert all(value == 0.0 for value in decision.targets.values())
    assert any("non promu" in note for note in decision.notes)


@pytest.mark.parametrize("problem", ["missing", "duplicate_declared", "duplicate_library", "empty"])
def test_factor_feature_contract_rejects_ambiguous_or_missing_columns(cfg_small, tmp_path, problem):
    index = pd.date_range("2026-01-01", periods=2, freq="15min", tz="UTC")
    frame = pd.DataFrame({"A": [1.0, 2.0]}, index=index)
    feats = FeatureSet({"iret_240m": frame, "iret_1440m": frame * 2})
    bundle = build_factor_bundle(cfg_small, "reversal")
    if problem == "missing":
        del feats.frames["iret_1440m"]
    elif problem == "duplicate_declared":
        bundle.feature_names = ["iret_240m", "iret_240m"]
    elif problem == "duplicate_library":
        feats.market["iret_240m"] = frame["A"]
    else:
        bundle.feature_names = []
    eng = _engine(cfg_small, bundle, tmp_path)
    with pytest.raises(RuntimeError, match=r"feature|unique"):
        eng._score_inputs(feats, frame.notna(), rows=np.array([1]))


def test_ml_bundles_still_require_the_complete_feature_list_in_order(cfg_small, tmp_path):
    index = pd.date_range("2026-01-01", periods=2, freq="15min", tz="UTC")
    frame = pd.DataFrame({"A": [1.0, 2.0]}, index=index)
    feats = FeatureSet({"iret_240m": frame, "iret_1440m": frame * 2, "extra": frame * 3})
    bundle = build_factor_bundle(cfg_small, "reversal")
    del bundle.meta["model_kind"]
    eng = _engine(cfg_small, bundle, tmp_path)
    with pytest.raises(RuntimeError, match="feature mismatch"):
        eng._score_inputs(feats, frame.notna(), rows=np.array([1]))
    bundle.feature_names = list(reversed(feats.names))
    with pytest.raises(RuntimeError, match="feature mismatch"):
        eng._score_inputs(feats, frame.notna(), rows=np.array([1]))


def test_factor_drift_columns_match_the_declared_subset(cfg_small, tmp_path, monkeypatch):
    cfg = with_overrides(cfg_small, {"features.max_lookback_minutes": 60, "features.vol_halflife_minutes": 15})
    index = pd.date_range("2026-01-01", periods=96 * 8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"A": np.arange(len(index)) / 1000, "B": np.arange(len(index)) / 2000}, index=index)
    feats = FeatureSet(
        frames={"unused": frame * 99, "first": frame, "last": frame * 2},
        market={"market_unused": frame["A"] * 99, "market_used": frame["A"] * 3},
    )
    bundle = build_factor_bundle(cfg, "reversal")
    # Interleaving the two feature types exposes accidental library-order or dimension mismatches.
    bundle.feature_names = ["market_used", "last", "first"]
    bundle.meta["feature_profile"] = {name: {} for name in bundle.feature_names}
    bundle.meta["drift_windows"] = {"contract_bars": 96, "market_bars": 96 * 7, "market": True}
    seen = []

    def capture(profile, blocks, *, calibrated):
        seen.extend(blocks)
        assert set(profile) == set(bundle.feature_names) and calibrated
        return {}, []

    monkeypatch.setattr("hermes.live.engine.drift_report", capture)
    eng = _engine(cfg, bundle, tmp_path)
    decision = Decision(str(index[-1]), 10_000, False, 2, 0.0)
    eng._check_drift(feats, frame.notna(), len(index) - 1, decision)
    assert [names for _, names in seen] == [["last", "first"], ["market_used"]]
    expected, _ = FeatureSet({"last": frame * 2, "first": frame}).stack(
        frame.notna(), rows=np.arange(len(index) - 96, len(index)), dtype=STORAGE_DTYPE
    )
    np.testing.assert_array_equal(seen[0][0], expected.astype(np.float32))
    np.testing.assert_array_equal(
        seen[1][0][:, 0], (frame["A"] * 3).iloc[-96 * 7 :].to_numpy(STORAGE_DTYPE).astype(np.float32)
    )
    assert decision.risk["psi_market"] == 1
