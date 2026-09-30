"""Selection is fixed before any confirmation/test outcomes are inspected."""

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from hermes.research.selection import DateWindow, SelectionProtocol, evaluate_candidates


@pytest.fixture
def protocol():
    return SelectionProtocol(
        calibration=DateWindow("2025-10-01", "2025-10-31"),
        confirmation=DateWindow("2025-11-01", "2025-11-30"),
        test=DateWindow("2025-12-01", "2025-12-31"),
        min_observations=20,
        bootstrap_samples=1000,
        mean_block_days=3,
    )


@pytest.fixture
def daily():
    index = pd.date_range("2025-10-01", "2025-12-31", tz="UTC")
    return pd.DataFrame({"steady": 0.001, "weak": -0.001}, index=index)


def test_future_losses_and_winners_cannot_replace_calibration_choice(protocol, daily):
    first = evaluate_candidates(daily, protocol)
    changed = daily.copy()
    changed.loc["2025-11-01":, "steady"] = -0.04
    changed.loc["2025-11-01":, "weak"] = 0.08
    second = evaluate_candidates(changed, protocol)
    assert first["selected"] == second["selected"] == "steady"
    assert first["best_calibration_candidate"] == second["best_calibration_candidate"] == "steady"
    assert first["candidates"]["steady"]["calibration"] == second["candidates"]["steady"]["calibration"]
    assert first["holdout_verdict"] == "positive_in_recorded_windows"
    assert second["holdout_verdict"] == "insufficient_holdout_evidence"
    assert not first["promotion_eligible"] and not second["promotion_eligible"]
    assert not first["holdout_is_globally_unexamined"]
    json.dumps(second, allow_nan=False)


def test_missing_and_invalid_future_values_affect_verdict_only(protocol, daily):
    first = evaluate_candidates(daily, protocol)
    changed = daily.drop(pd.Timestamp("2025-12-05", tz="UTC"))
    changed.loc["2025-11-10", "steady"] = np.nan
    changed.loc["2025-11-11", "steady"] = np.inf
    changed.loc["2025-11-12", "steady"] = -1.1
    second = evaluate_candidates(changed, protocol)
    assert first["selected"] == second["selected"] == "steady"
    assert first["calibration_ranking"] == second["calibration_ranking"]
    assert second["holdout_verdict"] == "insufficient_holdout_evidence"
    assert second["candidates"]["steady"]["confirmation"]["invalid_days"] == 3
    assert second["candidates"]["steady"]["test"]["missing_days"] == 1
    assert second["candidates"]["steady"]["test"]["net_return"] is None
    json.dumps(second, allow_nan=False)


def test_precalibration_history_and_future_absence_cannot_change_selection(protocol, daily):
    first = evaluate_candidates(daily, protocol)
    prefix = pd.DataFrame({"steady": -0.8, "weak": 2.0}, index=pd.date_range("2025-01-01", "2025-09-30", tz="UTC"))
    with_prefix = evaluate_candidates(pd.concat([prefix, daily]), protocol)
    calibration_only = evaluate_candidates(daily.loc[:"2025-10-31"], protocol)
    assert first["selected"] == with_prefix["selected"] == calibration_only["selected"] == "steady"
    assert calibration_only["holdout_verdict"] == "insufficient_holdout_evidence"
    assert calibration_only["candidates"]["steady"]["test"]["observations"] == 0


def test_lexical_ties_and_seed_are_independent_of_column_order(protocol, daily):
    same = daily.assign(zeta=0.001, alpha=0.001).drop(columns="weak")
    first = evaluate_candidates(same, protocol)
    second = evaluate_candidates(same[same.columns[::-1]], protocol)
    assert first == second
    assert first["selected"] == "alpha"
    assert first["calibration_ranking"] == ["alpha", "steady", "zeta"]


def test_cash_fallback_still_reports_exploratory_best_and_every_trial(protocol, daily):
    unprofitable = daily * 0 - 0.001
    unprofitable["weak"] = -0.002
    report = evaluate_candidates(unprofitable, protocol)
    assert report["selected"] == "cash"
    assert report["best_calibration_candidate"] == "steady"
    assert report["holdout_verdict"] == "cash_no_calibration_edge"
    assert set(report["candidates"]) == set(daily.columns)
    assert all(set(windows) == {"calibration", "confirmation", "test"} for windows in report["candidates"].values())


def test_positive_observed_return_does_not_imply_statistical_evidence(protocol, daily):
    changed = daily[["steady"]].copy()
    noisy = np.tile([-0.03, 0.034], 16)[:31]
    changed.loc[:"2025-10-31", "steady"] = noisy
    report = evaluate_candidates(changed, protocol)
    calibration = report["candidates"]["steady"]["calibration"]
    assert calibration["net_return"] > 0
    assert calibration["lower_mean_daily_return"] < 0
    assert report["selected"] == "cash" and report["best_calibration_candidate"] == "steady"
    assert "positive_mean_not_demonstrated" in calibration["reasons"]


def test_cost_stress_can_reject_candidate_without_changing_exploratory_ranking(protocol, daily):
    stress = daily - 0.002
    report = evaluate_candidates(daily, protocol, stressed_daily_returns=stress)
    assert report["selected"] == "cash" and report["best_calibration_candidate"] == "steady"
    assert "stressed_net_return_not_positive_or_unavailable" in report["candidates"]["steady"]["calibration"]["reasons"]
    future_stress = daily.copy()
    future_stress.loc["2025-11-01":] = -0.01
    changed = evaluate_candidates(daily, protocol, stressed_daily_returns=future_stress)
    assert changed["selected"] == "steady"
    assert changed["holdout_verdict"] == "insufficient_holdout_evidence"


def test_missing_stress_future_does_not_invalidate_calibration_choice(protocol, daily):
    report = evaluate_candidates(daily, protocol, stressed_daily_returns=daily.loc[:"2025-10-31"])
    assert report["selected"] == "steady"
    assert report["holdout_verdict"] == "insufficient_holdout_evidence"
    assert report["candidates"]["steady"]["confirmation"]["stress"]["missing_days"] == 30


def test_bonferroni_reserves_whole_family_and_discloses_prior_trials(protocol, daily):
    report = evaluate_candidates(daily, replace(protocol, prior_trials=14))
    assert report["candidates_evaluated"] == 2
    assert report["multiplicity_trials"] == 20
    assert report["adjusted_alpha"] == pytest.approx(0.05 / 20)
    assert report["candidates"]["steady"]["calibration"]["lower_bound_alpha"] == report["adjusted_alpha"]
    solo = evaluate_candidates(daily[["steady"]], protocol)
    assert solo["adjusted_alpha"] == pytest.approx(0.05 / 6)
    assert solo["protocol_sha256"] != report["protocol_sha256"]


def test_observations_and_daily_coverage_are_independent_requirements(protocol, daily):
    missing = daily.drop(pd.Timestamp("2025-10-15", tz="UTC"))
    report = evaluate_candidates(missing, protocol)
    calibration = report["candidates"]["steady"]["calibration"]
    assert calibration["observations"] == 30 > protocol.min_observations
    assert calibration["missing_days"] == 1
    assert calibration["lower_mean_daily_return"] is None
    assert report["selected"] == "cash"
    short = evaluate_candidates(daily, replace(protocol, min_observations=40))
    assert short["candidates"]["steady"]["calibration"]["net_return"] > 0
    assert "insufficient_observations" in short["candidates"]["steady"]["calibration"]["reasons"]


def test_first_day_loss_is_part_of_drawdown(protocol, daily):
    changed = daily.copy()
    changed.loc["2025-11-01", "steady"] = -0.1
    report = evaluate_candidates(changed, protocol)
    assert report["candidates"]["steady"]["confirmation"]["max_drawdown"] == pytest.approx(-0.1)


@pytest.mark.parametrize("kind", ["intraday", "duplicate", "unsorted", "non_datetime"])
def test_invalid_daily_index_is_rejected(protocol, daily, kind):
    changed = daily.copy()
    if kind == "intraday":
        changed.index += pd.Timedelta(hours=12)
    elif kind == "duplicate":
        changed = pd.concat([changed.iloc[:1], changed])
    elif kind == "unsorted":
        changed = changed.iloc[::-1]
    else:
        changed.index = changed.index.astype(str)
    with pytest.raises(ValueError):
        evaluate_candidates(changed, protocol)


def test_naive_daily_dates_are_utc_and_input_is_not_mutated(protocol, daily):
    original = daily.copy(deep=True)
    naive = daily.copy()
    naive.index = naive.index.tz_localize(None)
    assert evaluate_candidates(daily, protocol) == evaluate_candidates(naive, protocol)
    pd.testing.assert_frame_equal(daily, original)
    assert naive.index.tz is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"family_max": 7},
        {"prior_trials": -1},
        {"prior_trials": 1.5},
        {"min_observations": 1},
        {"alpha": 0},
        {"mean_block_days": np.nan},
        {"bootstrap_samples": 99},
        {"confirmation": DateWindow("2025-10-31", "2025-11-30")},
    ],
)
def test_protocol_rejects_invalid_or_overlapping_windows(protocol, kwargs):
    with pytest.raises(ValueError):
        replace(protocol, **kwargs)


def test_full_family_is_required_and_cash_name_reserved(protocol, daily):
    with pytest.raises(ValueError, match="family_max"):
        evaluate_candidates(daily, replace(protocol, family_max=1))
    with pytest.raises(ValueError, match="same candidate family"):
        evaluate_candidates(daily, protocol, stressed_daily_returns=daily[["steady"]])
    with pytest.raises(ValueError, match="cash is reserved"):
        evaluate_candidates(daily.rename(columns={"steady": "cash"}), protocol)


def test_window_dates_require_unambiguous_canonical_format():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        DateWindow("20251001", "2025-10-31")
    with pytest.raises(ValueError, match="start"):
        DateWindow("2025-11-01", "2025-10-31")


def test_selection_can_choose_lower_ranked_candidate_that_passes_cost_stress(protocol, daily):
    candidates = daily.assign(weak=0.0008)
    stressed = candidates.copy()
    stressed["steady"] = -0.0001
    report = evaluate_candidates(candidates, protocol, stressed_daily_returns=stressed)
    assert report["best_calibration_candidate"] == "steady"
    assert report["selected"] == "weak"
    assert report["holdout_verdict"] == "positive_in_recorded_windows"
    assert not report["promotion_eligible"]


def test_nonrepresentable_compounding_is_unavailable_not_a_winner(protocol, daily):
    changed = daily[["steady"]] * 0 + 1e20
    report = evaluate_candidates(changed, protocol)
    assert report["selected"] == "cash"
    assert report["candidates"]["steady"]["calibration"]["net_return"] is None
    json.dumps(report, allow_nan=False)
