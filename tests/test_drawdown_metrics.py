"""Le capital engagé avant la première perte fait partie du plus haut historique."""

import numpy as np
import pandas as pd
import pytest

from hermes.validation.metrics import max_drawdown, performance_summary, yearly_breakdown


@pytest.mark.parametrize(
    ("returns", "expected"),
    [([-0.2, 0.0], -0.2), ([-0.2, -0.1], -0.28), ([-0.2, 0.3, -0.1], -0.2), ([0.1, -0.3], -0.3)],
)
def test_performance_includes_drawdown_from_initial_capital(returns, expected):
    index = pd.date_range("2025-01-01", periods=len(returns), tz="UTC")
    summary = performance_summary(pd.Series(returns, index=index), 365)
    assert summary["max_drawdown"] == pytest.approx(expected)


def test_one_loss_is_measured_against_explicit_initial_capital():
    assert max_drawdown(pd.Series([800.0]), initial_equity=1000.0) == pytest.approx(-0.2)


def test_yearly_drawdown_restarts_from_capital_at_year_open():
    index = pd.to_datetime(["2024-12-30", "2024-12-31", "2025-01-01", "2025-01-02"], utc=True)
    result = yearly_breakdown(pd.Series([0.1, 0.1, -0.2, 0.0], index=index), 365)
    assert result.loc[2024, "max_drawdown"] == 0
    assert result.loc[2025, "max_drawdown"] == pytest.approx(-0.2)


@pytest.mark.parametrize("initial", [0, -1, np.nan, np.inf])
def test_initial_capital_must_be_finite_and_positive(initial):
    with pytest.raises(ValueError, match="capital initial"):
        max_drawdown(pd.Series([1.0]), initial_equity=initial)
