"""Provenance des données et du code qui produisent les prédictions hors échantillon.

Une dimension identique ne prouve pas que les observations sont identiques. La reprise exige les mêmes
octets d'entrée, sources et versions numériques ; une période tronquée est recalculée, car son dernier
bloc peut produire des labels différents. Aucun ancien marqueur incomplet n'est accepté.
"""

from __future__ import annotations

import hashlib
import json
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np

from hermes.research.dataset import Dataset

PROVENANCE_VERSION = 1
TRAINING_PATHS = (
    "config.py",
    "data",
    "features",
    "labels",
    "models",
    "portfolio/costs.py",
    "research/dataset.py",
    "research/walkforward.py",
    "research/run.py",
    "research/provenance.py",
    "validation/splits.py",
)
NUMERICAL_PACKAGES = ("numpy", "pandas", "scipy", "scikit-learn", "lightgbm", "numba", "torch")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def array_sha256(values: np.ndarray) -> str:
    """Empreinte des valeurs et de leur représentation, par blocs pour borner la mémoire temporaire."""
    a = np.asarray(values)
    if a.dtype.hasobject:
        raise ValueError("la provenance attend des tableaux numériques sans objets Python")
    digest = hashlib.sha256(_json({"shape": a.shape, "dtype": a.dtype.str}).encode())
    for start in range(0, len(a), 4096):
        digest.update(np.ascontiguousarray(a[start : start + 4096]).tobytes())
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def training_sources(root: Path | None = None) -> dict[str, str]:
    """Sources installées effectivement exécutées, y compris les modifications non commitées."""
    root = root or Path(__file__).resolve().parents[1]
    files: list[Path] = []
    for relative in TRAINING_PATHS:
        path = root / relative
        if not path.exists():
            raise ValueError(f"source d'entraînement absente : {relative}")
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return {str(path.relative_to(root)): file_sha256(path) for path in sorted(files)}


def numerical_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for name in NUMERICAL_PACKAGES:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def training_provenance(ds: Dataset, config_hash: str) -> dict[str, object]:
    panel = ds.panel
    # Le panneau conserve la source brute ; les matrices couvrent aussi l'univers et tout prétraitement.
    body: dict[str, object] = {
        "version": PROVENANCE_VERSION,
        "training_config_hash": config_hash,
        "panel": {
            "bar": panel.bar,
            "start": str(panel.index[0]),
            "end": str(panel.index[-1]),
            "symbols": panel.symbols,
            "index_sha256": array_sha256(panel.index.as_unit("ns").asi8),
            "fields": {name: array_sha256(frame.to_numpy()) for name, frame in sorted(panel.fields.items())},
        },
        "dataset": {
            "feature_names": ds.feature_names,
            "market_names": ds.market_names,
            "arrays": {
                name: array_sha256(getattr(ds, name))
                for name in ("X", "y", "y_raw", "t_pos", "s_pos", "market_X", "market_y")
            },
            "universe_sha256": array_sha256(ds.mask.to_numpy()),
        },
        "sources": training_sources(),
        "versions": numerical_versions(),
    }
    return {**body, "sha256": hashlib.sha256(_json(body).encode()).hexdigest()}


def provenance_json(provenance: dict[str, object]) -> str:
    return _json(provenance)


def same_training_inputs(saved: str, current: str) -> bool:
    """Refus fermé des marqueurs anciens, incomplets, modifiés ou d'une autre période."""
    try:
        previous, present = json.loads(saved), json.loads(current)
        required = {"version", "training_config_hash", "panel", "dataset", "sources", "versions", "sha256"}
        for manifest in (previous, present):
            if not isinstance(manifest, dict) or set(manifest) != required:
                return False
            if manifest["version"] != PROVENANCE_VERSION:
                return False
            body = {key: value for key, value in manifest.items() if key != "sha256"}
            if hashlib.sha256(_json(body).encode()).hexdigest() != manifest["sha256"]:
                return False
        return previous == present
    except (TypeError, ValueError):
        return False
