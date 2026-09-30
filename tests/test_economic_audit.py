"""Le bilan distingue gains simulés, preuve statistique et mouvements de solde."""

import json

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from hermes.cli import app
from hermes.research.audit import audit_economics


@pytest.fixture
def inputs(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"config_hash": "abc", "evaluation": {"promoted": False, "gate": {}}}))
    pd.DataFrame({"return": [-0.1, 0.02, 0.01]}, index=pd.date_range("2026-01-01", periods=3, tz="UTC")).to_csv(
        tmp_path / "equity_daily.csv"
    )
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "mode": "paper",
                "bundle": {"config_hash": "abc", "promoted": True},
                "account": {"initial": 10000, "equity": 9360, "fees_paid": 20, "funding_paid": -10},
                "ic_est": 0,
                "risk": {"ic_sizing": 0.04},
            }
        )
    )
    return report, status


def test_audit_reconciles_paper_loss_without_claiming_gross_alpha(inputs):
    report, status = inputs
    result = audit_economics(report, status)
    obs = result["prospective"]
    assert obs["net_pnl"] == -640
    assert obs["net_return"] == pytest.approx(-0.064)
    assert obs["funding_pnl"] == 10
    assert obs["price_and_execution_pnl"] == -630
    assert obs["price_and_execution_pnl"] + obs["funding_pnl"] - obs["fees_paid"] == obs["net_pnl"]
    assert obs["paper_nominal_sizing"] and obs["current_bundle_matches_report"]
    assert obs["pnl_assumption"] == "unchanged_initial_capital_and_no_external_flows"
    assert obs["assessment"] == "LOSING_OBSERVED"
    full = result["historical"]["windows"]["all"]
    assert full["net_return"] == pytest.approx(0.9 * 1.02 * 1.01 - 1)
    assert full["max_drawdown"] == pytest.approx(-0.1)
    assert full["sharpe"] is None  # trois jours ne donnent pas un annualisé utile
    assert not result["historical"]["promoted_at_evaluation"]
    assert obs["promoted_in_status"]  # le diagnostic conserve les verdicts datés


def test_live_balance_is_not_profit_and_unmatched_model_is_visible(inputs):
    report, status = inputs
    data = json.loads(status.read_text())
    data.update(mode="live", bundle={"config_hash": "other"})
    status.write_text(json.dumps(data))
    obs = audit_economics(report, status)["prospective"]
    assert obs["net_pnl"] is None and obs["assessment"] == "UNKNOWN"
    assert not obs["current_bundle_matches_report"]


@pytest.mark.parametrize("values", [[0.01, np.nan], [0.01, np.inf], [-1.01, 0.01]])
def test_audit_rejects_invalid_returns(inputs, values):
    report, _ = inputs
    pd.DataFrame({"return": values}, index=pd.date_range("2026-01-01", periods=2, tz="UTC")).to_csv(
        report.with_name("equity_daily.csv")
    )
    with pytest.raises(ValueError, match="rendements"):
        audit_economics(report)


def test_gaps_are_visible_and_do_not_count_as_zero_return_days(inputs):
    report, _ = inputs
    daily = pd.DataFrame({"return": [0.01, -0.02]}, index=["2026-01-01", "2026-01-04"])
    daily.to_csv(report.with_name("equity_daily.csv"))
    full = audit_economics(report)["historical"]["windows"]["all"]
    assert full["days"] == 2 and full["missing_calendar_days"] == 2
    assert full["sharpe"] is None
    daily.index = ["2026-01-01", "2026-01-01"]
    daily.to_csv(report.with_name("equity_daily.csv"))
    with pytest.raises(ValueError, match="dupliquées"):
        audit_economics(report)


def test_positive_paper_snapshot_is_observation_only(inputs):
    report, status = inputs
    data = json.loads(status.read_text())
    data["account"]["equity"] = 10100
    status.write_text(json.dumps(data))
    result = audit_economics(report, status)
    assert result["prospective"]["assessment"] == "POSITIVE_OBSERVED"
    assert any("ne démontre pas" in item for item in result["limits"])
    assert audit_economics(report)["prospective"] is None


def test_audit_rejects_csv_from_another_report(inputs):
    report, _ = inputs
    data = json.loads(report.read_text())
    data["evaluation"]["summary"] = {"total_return": 0.4}
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="même rendement"):
        audit_economics(report)
    data["evaluation"]["summary"]["total_return"] = 0.9 * 1.02 * 1.01 - 1 + 1e-7
    report.write_text(json.dumps(data))
    assert audit_economics(report)["historical"]["windows"]["all"]["days"] == 3


def test_cli_records_hashes_without_overwriting_evidence(inputs, tmp_path):
    report, status = inputs
    before = status.read_bytes()
    runner = CliRunner()
    result = runner.invoke(app, ["research", "audit", str(report), "--status", str(status)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["sources"]["status"]["sha256"]) == 64
    result = runner.invoke(app, ["research", "audit", str(report), "--out", str(status)])
    assert result.exit_code == 2
    assert status.read_bytes() == before
    out = tmp_path / "new" / "audit.json"
    result = runner.invoke(app, ["research", "audit", str(report), "--out", str(out)])
    assert result.exit_code == 0 and json.loads(out.read_text())["schema_version"] == 1
