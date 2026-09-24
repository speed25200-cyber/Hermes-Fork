import numpy as np
import pandas as pd

from hermes.research.evaluate import walk_forward_selection


def _days(start, n):
    return pd.date_range(start, periods=n, freq="1D", tz="UTC")


def test_selection_uses_only_the_past_and_pays_for_switches():
    idx = _days("2024-01-01", 800)
    rng = np.random.default_rng(0)
    a = pd.Series(0.0003 + 0.002 * rng.standard_normal(len(idx)), index=idx)  # modest, steady (Sharpe ~0.15/day)
    b = pd.Series(0.0, index=idx)
    spike = (idx >= "2025-06-01") & (idx < "2025-07-01")
    b[spike] += 0.05  # B earns only in June 2025: a trailing-year Sharpe of ~0.3/day once June is past
    grid = pd.DataFrame({"A": a, "B": b})
    default = pd.Series(0.0, index=idx)
    sel = walk_forward_selection(grid, default, lookback_days=365, min_days=180, switch_cost=0.001)
    ch = sel["choices"]
    assert ch["2024-01"] == "default" and ch["2024-06"] == "default"  # fewer than 180 days of history
    assert ch["2024-08"] == "A"
    assert ch["2025-06"] == "A"  # June's own returns cannot make June pick B
    assert ch["2025-07"] == "B"  # once June is in the past, B's trailing Sharpe leads
    r = sel["daily"]
    first_jul = r.index[r.index >= "2025-07-01"][0]
    assert abs(r[first_jul] - (b[first_jul] - 0.001)) < 1e-12  # a switch pays its cost on the first day
    mid = r.index[(r.index >= "2025-06-10")][0]
    assert abs(r[mid] - a[mid]) < 1e-12  # during June the book holds A, not the spiking B
    assert sel["switches"] >= 2 and set(sel["share"]) <= {"default", "A", "B"}


def test_choosing_the_configured_book_is_no_switch():
    idx = _days("2024-01-01", 500)
    own = pd.Series(0.001, index=idx) + pd.Series(np.random.default_rng(1).normal(0, 0.001, len(idx)), index=idx)
    grid = pd.DataFrame({"own": own, "worse": own - 0.002})
    sel = walk_forward_selection(grid, own, default_key="own", switch_cost=0.01)
    assert set(sel["choices"].values()) == {"default"} and sel["switches"] == 0
    assert np.allclose(sel["daily"].to_numpy(), own.to_numpy())
