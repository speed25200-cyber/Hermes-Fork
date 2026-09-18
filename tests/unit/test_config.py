import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from okxq.config import load_config
from okxq.config.live_guard import ApprovalManifest, verify_live_authorization
from okxq.config.modes import Mode
from okxq.domain.errors import ConfigError, LiveGuardError

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["base", "paper", "shadow", "demo", "live.disabled", "risk.research"])
def test_shipped_profiles_load(name):
    cfg = load_config(ROOT / "configs" / f"{name}.yaml")
    assert cfg.config_hash
    assert cfg.project.live_enabled is False


def _write(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load((ROOT / "configs" / "base.yaml").read_text())
    mutate(raw)
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def test_unknown_field_rejected(tmp_path):
    p = _write(tmp_path, lambda r: r["risk"].__setitem__("max_leverage", 50))
    with pytest.raises(ConfigError):
        load_config(p)


def test_secret_in_yaml_rejected(tmp_path):
    p = _write(tmp_path, lambda r: r["jev"].__setitem__("api_key", "sk-123"))
    with pytest.raises(ConfigError, match="secret"):
        load_config(p)


def test_live_enabled_outside_live_mode_rejected(tmp_path):
    p = _write(tmp_path, lambda r: r["project"].__setitem__("live_enabled", True))
    with pytest.raises(ConfigError):
        load_config(p)


def test_market_entries_require_crisis_policy(tmp_path):
    p = _write(tmp_path, lambda r: r["execution"].__setitem__("allow_market_entries", True))
    with pytest.raises(ConfigError):
        load_config(p)


def test_fixture_config_cannot_load_outside_paper(tmp_path):
    def mut(r):
        r["project"]["fixture_only"] = True
        r["project"]["mode"] = "DEMO"

    p = _write(tmp_path, mut)
    with pytest.raises(ConfigError):
        load_config(p)


def test_T64_live_without_manifest_is_refused(tmp_path):
    cfg = load_config(ROOT / "configs" / "live.disabled.yaml")
    with pytest.raises(LiveGuardError):
        verify_live_authorization(
            cfg, manifest_path=None, operator_secret="s", now=datetime.now(tz=UTC), code_commit="abc"
        )


def _live_cfg(tmp_path: Path):
    raw = yaml.safe_load((ROOT / "configs" / "base.yaml").read_text())
    raw["project"]["mode"] = "LIVE"
    raw["project"]["live_enabled"] = True
    raw["account"]["scope"] = "okx-live-test"
    raw["account"]["real_capital_usdt"] = "1000"
    p = tmp_path / "live.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p)


def _manifest(cfg, secret: str, tmp_path: Path, **over) -> Path:
    body = {
        "account_scope": cfg.account.scope,
        "environment": "LIVE",
        "code_commit": "abc123",
        "config_hash": cfg.config_hash,
        "artifact_hashes": {"model": "deadbeef"},
        "limits": {"max_gross_equity_multiple": 1.0},
        "issued_at": "2026-09-18T00:00:00+00:00",
        "expires_at": (datetime.now(tz=UTC) + timedelta(days=1)).isoformat(),
        "actor": "operator@example",
        "gates": {
            "technical": {"passed": True},
            "scientific": {"passed": True},
            "operator": {"passed": True},
        },
    }
    body.update(over)
    sig = ApprovalManifest.sign(body, secret)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"body": body, "signature": sig}))
    return p


def test_T64_live_manifest_must_be_signed_complete_and_unexpired(tmp_path):
    cfg = _live_cfg(tmp_path)
    assert cfg.mode is Mode.LIVE
    now = datetime.now(tz=UTC)
    ok = _manifest(cfg, "secret", tmp_path)
    proofs = verify_live_authorization(
        cfg, manifest_path=ok, operator_secret="secret", now=now, code_commit="abc123"
    )
    assert any("HMAC" in p for p in proofs)
    with pytest.raises(LiveGuardError):  # mauvais secret
        verify_live_authorization(
            cfg, manifest_path=ok, operator_secret="other", now=now, code_commit="abc123"
        )
    with pytest.raises(LiveGuardError):  # commit différent
        verify_live_authorization(cfg, manifest_path=ok, operator_secret="secret", now=now, code_commit="zzz")
    bad_gate = _manifest(cfg, "secret", tmp_path, gates={"technical": {"passed": True}})
    with pytest.raises(LiveGuardError):
        verify_live_authorization(
            cfg, manifest_path=bad_gate, operator_secret="secret", now=now, code_commit="abc123"
        )
    expired = _manifest(cfg, "secret", tmp_path, expires_at=(now - timedelta(seconds=1)).isoformat())
    with pytest.raises(LiveGuardError):
        verify_live_authorization(
            cfg, manifest_path=expired, operator_secret="secret", now=now, code_commit="abc123"
        )
    tampered = _manifest(cfg, "secret", tmp_path)
    doc = json.loads(tampered.read_text())
    doc["body"]["limits"]["max_gross_equity_multiple"] = 5.0
    tampered.write_text(json.dumps(doc))
    with pytest.raises(LiveGuardError):
        verify_live_authorization(
            cfg, manifest_path=tampered, operator_secret="secret", now=now, code_commit="abc123"
        )
