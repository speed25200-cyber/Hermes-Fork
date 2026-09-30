"""The factor research driver must register attempts before seeing any simulated results."""

import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from hermes.config import load_config
from hermes.data.synthetic import make_synthetic_panel
from hermes.models.bundle import ModelBundle
from hermes.models.factors import FACTOR_NAMES
from hermes.research import factor_research as research
from hermes.research.selection import DateWindow, SelectionProtocol


@pytest.fixture
def inputs():
    cfg = load_config(
        "configs/research_30m_xl_lb_sres_books.yaml",
        **{
            "portfolio.books": [],
            "data.source": "synthetic",
            "data.universe.venue": "any",
            "data.universe.min_history_days": 1,
            "data.universe.liquidity_lookback_days": 2,
            "data.universe.top_n": 8,
        },
    )
    panel = make_synthetic_panel(
        n_assets=8, n_bars=48 * 10, bar="30m", start="2025-01-01", seed=17, staggered_listings=False
    )
    protocol = SelectionProtocol(
        calibration=DateWindow("2025-01-04", "2025-01-05"),
        confirmation=DateWindow("2025-01-06", "2025-01-07"),
        test=DateWindow("2025-01-08", "2025-01-09"),
        min_observations=2,
        bootstrap_samples=1000,
    )
    return cfg, panel, protocol


def _fake_result(index, candidate, cost_multiplier, capital):
    values = np.full(len(index), 1e-5)
    if candidate == "carry":
        values[index < pd.Timestamp("2025-01-06", tz="UTC")] = 2e-5
    if candidate == "combined":
        values[index >= pd.Timestamp("2025-01-06", tz="UTC")] = 0.002
    returns = pd.Series(values / cost_multiplier, index=index, name="return")
    equity = capital * (1.0 + returns).cumprod()
    return SimpleNamespace(
        returns=returns,
        equity=equity,
        stats=pd.DataFrame({"ret": returns, "gross": 0.5}),
        risk_events=[],
        summary=lambda periods_per_year: {"total_return": equity.iloc[-1] / capital - 1.0},
    )


def test_registers_entire_family_before_simulation_and_exports_same_bundles(inputs, tmp_path, monkeypatch):
    cfg, panel, protocol = inputs
    # Archive panels can carry millisecond units while generated date ranges use microseconds.
    for frame in panel.fields.values():
        frame.index = frame.index.as_unit("ms")
    out, ledger = tmp_path / "run", tmp_path / "ledger"
    calls = []
    signals = {}

    def simulate(panel, mask, aux, signal, config, *, capital, start, end, cost_multiplier):
        candidate = FACTOR_NAMES[len(calls) // 2]
        declaration = json.loads((out / "declaration.json").read_text())
        assert [rule["name"] for rule in declaration["rules"]] == list(FACTOR_NAMES)
        assert len(list((out / "trials").glob("*.json"))) == 6
        assert len(list(ledger.glob("*.json"))) == 6
        assert len(list((out / "models").glob("*/bundle.json"))) == 6
        assert declaration["config"] == json.loads(config.model_dump_json())
        assert not (out / "report.json").exists()
        if candidate in signals:
            assert signals[candidate] is signal  # costs ×2 replays exactly the same input forecasts
        signals[candidate] = signal
        calls.append((candidate, cost_multiplier))
        return _fake_result(
            panel.index[(panel.index >= start) & (panel.index <= end)], candidate, cost_multiplier, capital
        )

    monkeypatch.setattr(research, "run_backtest", simulate)
    report = research.run_factor_research(cfg, panel, out, protocol, ledger=ledger)
    assert calls == [(name, multiplier) for name in FACTOR_NAMES for multiplier in (1.0, 2.0)]
    assert report["synthetic"] and not report["promotion_eligible"]
    assert report["selection"]["best_calibration_candidate"] == "carry"
    pointer = json.loads((out / "shadow_best.json").read_text())
    assert pointer["candidate"] == "carry"  # combined's better later returns cannot replace the frozen choice
    assert pointer["activation"] == "none" and not pointer["promoted"]
    trial_records = [json.loads(path.read_text()) for path in ledger.glob("*.json")]
    hashes = {record["config_hash"] for record in trial_records}
    assert len(hashes) == 6
    for name in FACTOR_NAMES:
        restored = ModelBundle.load(out / "models" / name)
        assert restored.meta["config_hash"] in hashes
        assert restored.meta["factor_rule"] == name
        assert not restored.promoted and restored.prior_ic == 0.0
        score = pd.read_parquet(out / "local" / f"{name}-scores.parquet")
        assert research._frame_manifest(score) == report["inputs"]["scores"][name]
        for suffix in ("", "_cost2"):
            daily = pd.read_csv(out / "daily" / f"{name}{suffix}.csv", index_col=0)
            assert len(daily) == 6 and daily.columns.tolist() == ["net_return"]
    assert json.loads((out / "report.json").read_text()) == report
    assert (out / "local" / ".gitignore").read_text() == "*\n!.gitignore\n"


def test_failed_simulation_remains_registered_and_cannot_be_overwritten(inputs, tmp_path, monkeypatch):
    cfg, panel, protocol = inputs
    out = tmp_path / "failed"

    def fail(*args, **kwargs):
        assert len(list((out / "trials").glob("*.json"))) == 6
        raise RuntimeError("engine failed")

    monkeypatch.setattr(research, "run_backtest", fail)
    with pytest.raises(RuntimeError, match="engine failed"):
        research.run_factor_research(cfg, panel, out, protocol)
    declaration = (out / "declaration.json").read_bytes()
    assert not (out / "report.json").exists()
    with pytest.raises(ValueError, match="new or empty"):
        research.run_factor_research(cfg, panel, out, protocol)
    assert (out / "declaration.json").read_bytes() == declaration


def test_missing_market_bar_is_rejected_without_filling_zero_returns(inputs, tmp_path):
    cfg, panel, protocol = inputs
    panel = panel.iloc(slice(0, 48 * 9 - 1))
    with pytest.raises(ValueError, match="every bar"):
        research.run_factor_research(cfg, panel, tmp_path / "missing", protocol)
    assert not (tmp_path / "missing").exists()


def test_incomplete_engine_return_series_fails_before_selection(inputs, tmp_path, monkeypatch):
    cfg, panel, protocol = inputs

    def drop_bar(panel, mask, aux, signal, config, *, capital, start, end, cost_multiplier):
        index = panel.index[(panel.index >= start) & (panel.index <= end)][1:]
        return _fake_result(index, "carry", cost_multiplier, capital)

    monkeypatch.setattr(research, "run_backtest", drop_bar)
    out = tmp_path / "missing-return"
    with pytest.raises(ValueError, match="every declared simulation bar"):
        research.run_factor_research(cfg, panel, out, protocol)
    assert (out / "declaration.json").exists()
    assert not (out / "shadow_best.json").exists()


def test_whole_family_must_be_reserved(inputs, tmp_path):
    cfg, panel, protocol = inputs
    with pytest.raises(ValueError, match="all six"):
        research.run_factor_research(cfg, panel, tmp_path / "small-family", replace(protocol, family_max=5))


def test_atomic_registration_does_not_replace_existing_evidence(tmp_path):
    path = tmp_path / "record.json"
    research._write_json(path, {"original": True})
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        research._write_json(path, {"original": False})
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["record.json"]
