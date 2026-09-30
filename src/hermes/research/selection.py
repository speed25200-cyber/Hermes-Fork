"""One frozen, chronological comparison of an explicitly declared candidate family.

Inputs are *net* daily returns, including execution costs and funding. This module
never estimates those costs, tunes a strategy, writes a model or authorizes trading.
Only the calibration window can choose a candidate; subsequent windows diagnose
that frozen choice. Retrospective windows are not fresh evidence merely because
this function does not use them to select.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from hermes.validation.stats import stationary_bootstrap


@dataclass(frozen=True)
class DateWindow:
    """Inclusive calendar dates, interpreted as UTC daily observations."""

    start: str
    end: str

    def __post_init__(self) -> None:
        start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        if start.isoformat() != self.start or end.isoformat() != self.end:
            raise ValueError("window dates must use YYYY-MM-DD format")
        if start > end:
            raise ValueError("window start must not exceed its end")

    def index(self) -> pd.DatetimeIndex:
        return pd.date_range(self.start, self.end, freq="D", tz="UTC")


@dataclass(frozen=True)
class SelectionProtocol:
    """Declare this protocol and every candidate before inspecting their results.

    ``prior_trials`` counts earlier examined configurations, not only earlier
    winners. The Bonferroni divisor always reserves the whole candidate family.
    Bootstrap intervals are approximate and rely on a stationary return process;
    recording prior trials cannot remove earlier human exposure to market history.
    """

    calibration: DateWindow = DateWindow("2025-10-01", "2025-12-31")
    confirmation: DateWindow = DateWindow("2026-01-01", "2026-04-30")
    test: DateWindow = DateWindow("2026-05-01", "2026-08-31")
    family_max: int = 6
    prior_trials: int = 0
    min_observations: int = 60
    alpha: float = 0.05
    mean_block_days: float = 7.0
    bootstrap_samples: int = 10_000
    seed: int = 20260930
    examined_history: bool = True

    def __post_init__(self) -> None:
        if not self.calibration.end < self.confirmation.start <= self.confirmation.end < self.test.start:
            raise ValueError("calibration, confirmation and test windows must be chronological and disjoint")
        for name, minimum in (
            ("family_max", 1),
            ("prior_trials", 0),
            ("min_observations", 2),
            ("bootstrap_samples", 1000),
            ("seed", 0),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if self.family_max > 6:
            raise ValueError("declare at most six candidates in this comparison")
        if not np.isfinite(self.alpha) or not 0 < self.alpha < 1:
            raise ValueError("alpha must be between zero and one")
        if not np.isfinite(self.mean_block_days) or self.mean_block_days < 1:
            raise ValueError("mean_block_days must be finite and >= 1")
        if not isinstance(self.examined_history, bool):
            raise ValueError("examined_history must be boolean")

    @property
    def adjusted_alpha(self) -> float:
        return self.alpha / (self.family_max + self.prior_trials)


def _validate_frame(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(f"{name} requires a DatetimeIndex")
    if frame.index.hasnans or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError(f"{name} dates must be unique, sorted and nonmissing")
    index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")
    if not index.equals(index.normalize()):
        raise ValueError(f"{name} requires midnight UTC daily observations, not intraday returns")
    if not frame.columns.is_unique or any(not isinstance(c, str) or not c or c == "cash" for c in frame.columns):
        raise ValueError(f"{name} requires unique nonempty candidate names; cash is reserved")
    copy = frame.copy(deep=False)
    copy.index = index
    return copy


def _window_values(series: pd.Series, window: DateWindow) -> tuple[np.ndarray, dict]:
    expected = window.index()
    part = series.loc[(series.index >= expected[0]) & (series.index <= expected[-1])]
    values = pd.to_numeric(part, errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(values) & (values >= -1)
    observed = values[valid]
    complete = len(values) == len(expected) and bool(valid.all())
    with np.errstate(over="ignore", invalid="ignore"):
        compounded = float(np.prod(1 + observed) - 1) if len(observed) else None
    # Report an invalid numerical result as unavailable, never as JSON Infinity.
    compounded = compounded if compounded is None or np.isfinite(compounded) else None
    return observed, {
        "expected_days": len(expected),
        "observations": len(observed),
        "missing_days": len(expected) - len(values),
        "invalid_days": int((~valid).sum()),
        "complete": complete,
        "net_return": compounded if complete else None,
        "observed_net_return": compounded,
    }


def _assess(
    series: pd.Series,
    stress: pd.Series | None,
    window: DateWindow,
    protocol: SelectionProtocol,
) -> dict:
    values, report = _window_values(series, window)
    reasons = []
    if not report["complete"]:
        reasons.append("incomplete_or_invalid_daily_coverage")
    if len(values) < protocol.min_observations:
        reasons.append("insufficient_observations")
    if report["net_return"] is None or report["net_return"] <= 0:
        reasons.append("net_return_not_positive_or_unavailable")
    lower = None
    if report["complete"] and len(values) >= protocol.min_observations:
        means = stationary_bootstrap(
            values,
            np.mean,
            n_samples=protocol.bootstrap_samples,
            mean_block=protocol.mean_block_days,
            seed=protocol.seed,
        )
        lower = float(np.quantile(means, protocol.adjusted_alpha))
        if not np.isfinite(lower):
            lower = None
    if lower is None or lower <= 0:
        reasons.append("positive_mean_not_demonstrated")
    stress_report = None
    if stress is not None:
        _, stress_report = _window_values(stress, window)
        if not stress_report["complete"]:
            reasons.append("incomplete_or_invalid_stress_coverage")
        if stress_report["net_return"] is None or stress_report["net_return"] <= 0:
            reasons.append("stressed_net_return_not_positive_or_unavailable")
    with np.errstate(over="ignore", invalid="ignore"):
        equity = np.cumprod(1 + values)
        peak = np.maximum.accumulate(np.r_[1.0, equity])[1:]
        drawdown = float(np.min(equity / peak - 1)) if report["complete"] and len(values) else None
        mean = float(values.mean()) if len(values) else None
    report.update(
        window=asdict(window),
        mean_daily_return=mean if mean is None or np.isfinite(mean) else None,
        lower_mean_daily_return=lower,
        lower_bound_alpha=protocol.adjusted_alpha,
        max_drawdown=drawdown if drawdown is None or np.isfinite(drawdown) else None,
        stress=stress_report,
        passes=not reasons,
        reasons=reasons,
    )
    return report


def evaluate_candidates(
    daily_returns: pd.DataFrame,
    protocol: SelectionProtocol,
    *,
    stressed_daily_returns: pd.DataFrame | None = None,
) -> dict:
    """Return a JSON-compatible audit; select exclusively from calibration.

    Supply every attempted candidate as a column, including losing candidates.
    Missing days are never filled with zero. Zero is the explicit cash fallback,
    with no assumed cash yield. Stress input, when supplied, contains a separate
    cost-inclusive replay of the same candidates, not costs to subtract here.
    """
    daily = _validate_frame(daily_returns, "daily_returns")
    names = sorted(daily.columns)
    if not names or len(names) > protocol.family_max:
        raise ValueError("candidate count must be between one and the declared family_max")
    stress = None
    if stressed_daily_returns is not None:
        stress = _validate_frame(stressed_daily_returns, "stressed_daily_returns")
        if set(stress.columns) != set(names):
            raise ValueError("stress returns must include exactly the same candidate family")

    # Finish selection before reading any confirmation or test return values.
    calibration = {
        name: _assess(daily[name], None if stress is None else stress[name], protocol.calibration, protocol)
        for name in names
    }
    rankable = [
        name
        for name in names
        if calibration[name]["lower_mean_daily_return"] is not None and calibration[name]["net_return"] is not None
    ]
    ranking = sorted(
        rankable,
        key=lambda name: (
            -calibration[name]["lower_mean_daily_return"],
            -calibration[name]["net_return"],
            name,
        ),
    )
    best = ranking[0] if ranking else None
    eligible = [name for name in ranking if calibration[name]["passes"]]
    selected = eligible[0] if eligible else "cash"
    candidates = {}
    for name in names:
        candidates[name] = {"calibration": calibration[name]}
        for label in ("confirmation", "test"):
            candidates[name][label] = _assess(
                daily[name], None if stress is None else stress[name], getattr(protocol, label), protocol
            )
    if selected == "cash":
        verdict = "cash_no_calibration_edge"
    elif all(candidates[selected][label]["passes"] for label in ("confirmation", "test")):
        verdict = "positive_in_recorded_windows"
    else:
        verdict = "insufficient_holdout_evidence"
    protocol_dict = asdict(protocol)
    return {
        "schema_version": 1,
        "protocol": protocol_dict,
        "protocol_sha256": hashlib.sha256(json.dumps(protocol_dict, sort_keys=True).encode()).hexdigest(),
        "candidates_evaluated": len(names),
        "multiplicity_trials": protocol.family_max + protocol.prior_trials,
        "adjusted_alpha": protocol.adjusted_alpha,
        "bootstrap_expected_lower_tail_samples": protocol.bootstrap_samples * protocol.adjusted_alpha,
        "selected": selected,
        "best_calibration_candidate": best,
        "calibration_ranking": ranking,
        "selection_basis": "calibration_only_lower_mean_then_net_return_then_lexical_name",
        "selection_reason": "calibration_evidence_passed" if eligible else "no_candidate_passed_calibration",
        "holdout_verdict": verdict,
        "holdout_is_globally_unexamined": not protocol.examined_history,
        "promotion_eligible": False,
        "candidates": candidates,
        "limits": [
            "Returns must already include fees, funding and execution costs; this function cannot verify their origin.",
            "Bootstrap bounds are approximate, assume stationarity and do not establish future profitability.",
            "All trials must be disclosed; prior_trials does not undo earlier inspection of market history.",
            "The exploratory best candidate is not approved for trading, even when selected is cash.",
            "Confirmation and test results never choose a replacement or authorize automatic promotion.",
            "Already examined history is retrospective evidence, not a globally untouched holdout."
            if protocol.examined_history
            else "The caller, not this function, attests that the holdout history was previously unexamined.",
        ],
    }
