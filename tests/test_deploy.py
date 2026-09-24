"""deploy/challenger.sh: the one champion/challenger rule (runner retraining and retrain.sh)."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _bundle(d: Path, config_hash: str, promoted: bool) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "bundle.json").write_text(
        json.dumps({"config_hash": config_hash, "promoted": promoted, "train_end": "2026-09-01"})
    )
    return d


@pytest.fixture
def vps(tmp_path):
    """A fake /opt/hermes with a stub ``hermes`` that records ``model install`` calls."""
    log = tmp_path / "calls.log"
    stub = tmp_path / "hermes"
    stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "HERMES_ROOT": str(tmp_path), "HERMES_BIN": str(stub)}

    def run(new: Path):
        subprocess.run(["bash", str(ROOT / "deploy/challenger.sh"), str(new)], env=env, check=True, capture_output=True)
        return log.read_text().splitlines() if log.exists() else []

    return tmp_path, run


def test_first_champion_and_same_strategy_are_always_installed(vps):
    root, run = vps
    assert len(run(_bundle(root / "new1", "aaa", False))) == 1  # no champion yet
    _bundle(root / "artifacts/models/champion", "aaa", True)
    calls = run(_bundle(root / "new2", "aaa", False))  # same strategy, now failing the gate: demotion
    assert len(calls) == 2 and calls[-1].startswith("model install") and "artifacts/models/champion" in calls[-1]


def test_other_strategy_replaces_a_promoted_champion_only_if_promoted(vps):
    root, run = vps
    _bundle(root / "artifacts/models/champion", "aaa", True)
    assert run(_bundle(root / "new", "bbb", False)) == []  # the promoted champion stays
    assert len(run(_bundle(root / "new2", "bbb", True))) == 1
    _bundle(root / "artifacts/models/champion", "aaa", False)
    assert len(run(_bundle(root / "new3", "ccc", False))) == 2  # an unpromoted champion is always replaced


def test_same_strategy_is_recognised_across_code_versions(vps, tmp_path):
    """The recorded config hash depends on the code that trained the model; the rule compares identities
    recomputed by the installed code, so a promoted champion that now fails the gate is still demoted."""
    import sys

    from hermes.config import load_config

    root, _ = vps
    env_py = {**os.environ, "HERMES_ROOT": str(root), "HERMES_BIN": str(root / "hermes"), "HERMES_PY": sys.executable}
    cfg = load_config(None)
    champion = _bundle(root / "artifacts/models/champion", "hash-by-old-code", True)
    (champion / "config.json").write_text(cfg.model_dump_json())
    same = _bundle(root / "same", "hash-by-new-code", False)
    (same / "config.json").write_text(cfg.model_dump_json())
    other = _bundle(root / "other", "hash-by-new-code", False)
    other_cfg = cfg.model_copy(update={"portfolio": cfg.portfolio.model_copy(update={"cost_aversion": 3.0})})
    (other / "config.json").write_text(other_cfg.model_dump_json())

    def run(new):
        cmd = ["bash", str(ROOT / "deploy/challenger.sh"), str(new)]
        subprocess.run(cmd, env=env_py, check=True, capture_output=True)
        log = root / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    assert run(other) == []  # another strategy, unpromoted: the promoted champion stays
    assert len(run(same)) == 1  # the same strategy re-evaluated: it replaces (demotes) the champion


def test_model_install_swaps_the_champion_atomically(tmp_path):
    from typer.testing import CliRunner

    from hermes.cli import app

    src = tmp_path / "src"
    src.mkdir()
    (src / "bundle.json").write_text(json.dumps({"files": {}, "feature_names": [], "weights": {}, "prior_ic": 0.0}))
    from hermes.config import load_config

    (src / "config.json").write_text(load_config(None).model_dump_json())
    target = tmp_path / "champion"
    target.mkdir()
    (target / "old.txt").write_text("old")
    res = CliRunner().invoke(app, ["model", "install", str(src), "--to", str(target)])
    assert res.exit_code == 0, res.output
    assert (target / "bundle.json").exists() and (tmp_path / "champion.previous" / "old.txt").exists()
    assert not list(tmp_path.glob("champion.incoming-*"))
