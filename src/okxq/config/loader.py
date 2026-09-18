"""Chargement YAML avec héritage ``extends``, fusion profonde et validation stricte.

Les secrets n'ont pas leur place dans un YAML : toute clé ressemblant à un secret fait échouer le chargement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from okxq.config.schema import AppConfig
from okxq.domain.errors import ConfigError
from okxq.domain.ids import payload_hash

_SECRET_MARKERS = ("api_key", "api_secret", "passphrase", "password", "token", "secret")
_MAX_EXTENDS_DEPTH = 5


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"fichier de configuration introuvable : {path}", path=str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path} : {exc}", path=str(path)) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} doit contenir un mapping à la racine", path=str(path))
    return raw


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _scan_secrets(node: Any, trail: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            k = str(key).lower()
            if any(marker in k for marker in _SECRET_MARKERS) and isinstance(value, str) and value.strip():
                raise ConfigError(
                    f"la clé {trail}{key} ressemble à un secret : les secrets vivent dans l'environnement, jamais en YAML",
                    key=f"{trail}{key}",
                )
            _scan_secrets(value, f"{trail}{key}.")
    elif isinstance(node, list):
        for item in node:
            _scan_secrets(item, trail)


def load_raw(path: str | Path, *, _depth: int = 0) -> dict[str, Any]:
    p = Path(path)
    raw = _read_yaml(p)
    extends = raw.pop("extends", None)
    if extends is not None:
        if _depth >= _MAX_EXTENDS_DEPTH:
            raise ConfigError("chaîne extends trop profonde", path=str(p))
        parent = load_raw(p.parent / str(extends), _depth=_depth + 1)
        raw = _deep_merge(parent, raw)
    return raw


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    raw = load_raw(p)
    _scan_secrets(raw)
    try:
        cfg = AppConfig.model_validate(raw)
    except ValueError as exc:  # pydantic.ValidationError hérite de ValueError
        raise ConfigError(f"configuration invalide ({p}) :\n{exc}", path=str(p)) from exc
    return cfg.model_copy(update={"source_path": str(p), "config_hash": payload_hash(raw)})
