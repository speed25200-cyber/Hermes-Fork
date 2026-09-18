"""Jeux de données golden (§68.2) : lecture/écriture de ``events.jsonl`` + ``manifest.json``.

Format : une ``EventEnvelope`` JSON par ligne (``model_dump(mode="json")``, clés triées), ordonnée par
``(available_at, ingest_seq)``. Le manifeste porte le SHA-256 du fichier d'événements : un replay vérifie
qu'il rejoue exactement le jeu annoncé.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope

__all__ = [
    "EVENTS_FILE",
    "MANIFEST_FILE",
    "DatasetManifest",
    "build_manifest",
    "load_manifest",
    "read_events",
    "sha256_file",
    "write_events",
    "write_manifest",
]

EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset: str
    schema_version: int
    instruments: list[str]
    first_available_at: str
    last_available_at: str
    rows: int
    sha256: str
    quality_level: str
    notes: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(directory: Path) -> DatasetManifest:
    path = Path(directory) / MANIFEST_FILE
    if not path.exists():
        raise DataQualityError(f"manifeste introuvable : {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        return DatasetManifest(
            dataset=str(raw["dataset"]),
            schema_version=int(raw["schema_version"]),
            instruments=[str(x) for x in raw["instruments"]],
            first_available_at=str(raw["first_available_at"]),
            last_available_at=str(raw["last_available_at"]),
            rows=int(raw["rows"]),
            sha256=str(raw["sha256"]),
            quality_level=str(raw["quality_level"]),
            notes=dict(raw.get("notes", {})),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataQualityError(f"manifeste invalide : {exc}") from exc


def read_events(path: Path, *, verify_sha256: str | None = None) -> Iterator[EventEnvelope]:
    """Itère les enveloppes validées ; refuse un fichier dont l'ordre ``(available_at, ingest_seq)`` recule."""
    p = Path(path)
    if not p.exists():
        raise DataQualityError(f"fichier d'événements introuvable : {p}")
    if verify_sha256 is not None:
        actual = sha256_file(p)
        if actual != verify_sha256:
            raise DataQualityError(
                "SHA-256 du jeu différent du manifeste", expected=verify_sha256, actual=actual
            )
    last_key: tuple[datetime, int] | None = None
    with p.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                env = EventEnvelope.model_validate_json(line)
            except ValidationError as exc:
                raise DataQualityError(f"ligne {line_no} invalide : {exc}") from exc
            key = (env.available_at, env.ingest_seq)
            if last_key is not None and key < last_key:
                raise DataQualityError(f"ligne {line_no} : ordre (available_at, ingest_seq) non croissant")
            last_key = key
            yield env


def write_events(path: Path, envelopes: Iterable[EventEnvelope]) -> tuple[int, str]:
    """Écrit les lignes (JSON compact, clés triées) ; renvoie (lignes, sha256)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        for env in envelopes:
            fh.write(
                json.dumps(
                    env.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
            )
            fh.write("\n")
            rows += 1
    return rows, sha256_file(p)


def build_manifest(
    *,
    dataset: str,
    instruments: Iterable[str],
    first_available_at: datetime,
    last_available_at: datetime,
    rows: int,
    sha256: str,
    quality_level: str,
    notes: dict[str, Any],
) -> DatasetManifest:
    return DatasetManifest(
        dataset=dataset,
        schema_version=SCHEMA_VERSION,
        instruments=sorted(instruments),
        first_available_at=ensure_utc(first_available_at).isoformat(),
        last_available_at=ensure_utc(last_available_at).isoformat(),
        rows=rows,
        sha256=sha256,
        quality_level=quality_level,
        notes=notes,
    )


def write_manifest(directory: Path, manifest: DatasetManifest) -> Path:
    path = Path(directory) / MANIFEST_FILE
    path.write_text(
        json.dumps(manifest.to_json(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path
