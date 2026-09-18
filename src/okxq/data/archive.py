"""Archive immuable des données brutes : JSONL golden et partitions Parquet (§6, §44).

Deux formats, un seul contrat. Le JSONL sert aux jeux golden versionnés et reproductibles ; le Parquet
compressé sert aux historiques volumineux. Dans les deux cas :

- les partitions sont **immuables** : on n'écrase jamais une partition écrite, on en crée une nouvelle ;
- un **manifeste** porte le dataset, le nombre de lignes, les bornes temporelles et un **checksum**,
  seul moyen de savoir qu'un fichier lu est bien celui qui a été écrit ;
- la déduplication se fait sur ``(event_type, inst_id, exchange_ts, ingest_seq)`` : un même événement
  relivré deux fois par un flux « au moins une fois » ne doit pas compter deux fois.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope

__all__ = [
    "MANIFEST_NAME",
    "PartitionRecord",
    "dedup_key",
    "event_inst_id",
    "manifest_path_for",
    "read_jsonl_events",
    "read_parquet_events",
    "verify_dataset_checksum",
    "write_jsonl_dataset",
    "write_parquet_partition",
]

MANIFEST_NAME: Final = "manifest.json"
EVENTS_NAME: Final = "events.jsonl"
NO_INSTRUMENT: Final = "_"


def event_inst_id(envelope: EventEnvelope) -> str:
    """Instrument porté par un événement, ou ``_`` quand il n'en porte pas (métadonnées globales)."""
    value = envelope.payload.get("inst_id")
    return str(value) if value else NO_INSTRUMENT


def dedup_key(envelope: EventEnvelope) -> tuple[str, str, str, int]:
    return (
        envelope.event_type,
        event_inst_id(envelope),
        envelope.exchange_ts.isoformat() if envelope.exchange_ts else "",
        envelope.ingest_seq,
    )


def deduplicate(events: Iterable[EventEnvelope]) -> list[EventEnvelope]:
    seen: set[tuple[str, str, str, int]] = set()
    out: list[EventEnvelope] = []
    for env in events:
        key = dedup_key(env)
        if key in seen:
            continue
        seen.add(key)
        out.append(env)
    return out


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path_for(directory: str | Path) -> Path:
    return Path(directory) / MANIFEST_NAME


def _bounds(events: Sequence[EventEnvelope]) -> tuple[str | None, str | None, str | None, str | None]:
    if not events:
        return None, None, None, None
    availables = sorted(e.available_at for e in events)
    exchanges = sorted(e.exchange_ts for e in events if e.exchange_ts is not None)
    return (
        availables[0].isoformat(),
        availables[-1].isoformat(),
        exchanges[0].isoformat() if exchanges else None,
        exchanges[-1].isoformat() if exchanges else None,
    )


def write_jsonl_dataset(
    events: Sequence[EventEnvelope],
    directory: str | Path,
    *,
    dataset: str,
    clock: Clock,
    quality_level: str,
    notes: str = "",
    schema_version: int = 1,
) -> dict[str, Any]:
    """Écrit ``events.jsonl`` + ``manifest.json``. Les événements sont triés ``(available_at, ingest_seq)``."""
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(deduplicate(events), key=lambda e: (e.available_at, e.ingest_seq))
    events_file = out_dir / EVENTS_NAME
    with events_file.open("w", encoding="utf-8") as handle:
        for env in ordered:
            handle.write(json.dumps(env.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n")
    first_av, last_av, first_ex, last_ex = _bounds(ordered)
    manifest: dict[str, Any] = {
        "dataset": dataset,
        "format": "jsonl",
        "schema_version": schema_version,
        "generated_at": ensure_utc(clock.now_utc()).isoformat(),
        "quality_level": quality_level,
        "rows": len(ordered),
        "instruments": sorted({event_inst_id(e) for e in ordered} - {NO_INSTRUMENT}),
        "event_types": sorted({e.event_type for e in ordered}),
        "first_available_at": first_av,
        "last_available_at": last_av,
        "first_exchange_ts": first_ex,
        "last_exchange_ts": last_ex,
        "files": [EVENTS_NAME],
        "sha256": _sha256_of_file(events_file),
        "notes": notes,
    }
    manifest_path_for(out_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def read_jsonl_events(path: str | Path) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []
    file = Path(path)
    if not file.exists():
        raise DataQualityError(f"fichier d'événements absent : {file}", path=str(file))
    for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(EventEnvelope.model_validate_json(line))
        except ValueError as exc:
            raise DataQualityError(
                f"ligne {number} illisible dans {file.name} : {exc}", path=str(file)
            ) from exc
    return events


def verify_dataset_checksum(directory: str | Path, manifest: Mapping[str, Any]) -> None:
    """Vérifie le checksum de chaque fichier déclaré. Un manifeste sans checksum est refusé."""
    out_dir = Path(directory)
    declared = manifest.get("sha256")
    files = list(manifest.get("files", [EVENTS_NAME]))
    if not declared:
        raise DataQualityError("manifeste sans sha256 : impossible de prouver l'intégrité")
    if len(files) == 1:
        actual = _sha256_of_file(out_dir / str(files[0]))
        if actual != declared:
            raise DataQualityError(
                "checksum du jeu de données différent du manifeste", expected=str(declared), actual=actual
            )
        return
    digest = hashlib.sha256()
    for name in files:
        digest.update(_sha256_of_file(out_dir / str(name)).encode("ascii"))
    if digest.hexdigest() != declared:
        raise DataQualityError("checksum agrégé différent du manifeste", expected=str(declared))


# --- Parquet -------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartitionRecord:
    """Ce qu'il faut inscrire dans ``raw_partitions`` (§44) pour retrouver et vérifier la partition."""

    dataset: str
    inst_id: str | None
    partition_key: str
    path: str
    checksum_sha256: str
    rows: int
    first_ts: datetime | None
    last_ts: datetime | None
    written_at: datetime
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "inst_id": self.inst_id,
            "partition_key": self.partition_key,
            "path": self.path,
            "checksum_sha256": self.checksum_sha256,
            "rows": self.rows,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "written_at": self.written_at.isoformat(),
            "schema_version": self.schema_version,
        }


def partition_key_for(envelope: EventEnvelope) -> str:
    """Clé de partition : type d'événement, instrument, heure UTC de disponibilité."""
    hour = envelope.available_at.strftime("%Y%m%dT%H")
    return f"{envelope.event_type}/{event_inst_id(envelope)}/{hour}"


def write_parquet_partition(
    events: Sequence[EventEnvelope],
    root: str | Path,
    *,
    dataset: str,
    clock: Clock,
    compression: str = "zstd",
    overwrite: bool = False,
) -> list[PartitionRecord]:
    """Écrit une partition Parquet par ``(event_type, inst_id, heure)``. N'écrase pas sans ``overwrite``."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    base = Path(root)
    grouped: dict[str, list[EventEnvelope]] = {}
    for env in deduplicate(events):
        grouped.setdefault(partition_key_for(env), []).append(env)

    records: list[PartitionRecord] = []
    now = ensure_utc(clock.now_utc())
    for key, group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda e: (e.available_at, e.ingest_seq))
        target = base / dataset / f"{key}.parquet"
        if target.exists() and not overwrite:
            raise DataQualityError(
                "partition déjà écrite : les partitions sont immuables", path=str(target), partition_key=key
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        table = pa.table(
            {
                "event_id": [e.event_id for e in ordered],
                "event_type": [e.event_type for e in ordered],
                "schema_version": [e.schema_version for e in ordered],
                "source": [e.source for e in ordered],
                "inst_id": [event_inst_id(e) for e in ordered],
                "exchange_ts": [e.exchange_ts for e in ordered],
                "receive_ts": [e.receive_ts for e in ordered],
                "available_at": [e.available_at for e in ordered],
                "ingest_seq": [e.ingest_seq for e in ordered],
                "payload_hash": [e.payload_hash for e in ordered],
                # Le payload reste du JSON : il porte des décimaux en chaînes, qu'aucun type colonne
                # ne doit convertir en flottant.
                "payload_json": [json.dumps(e.payload, ensure_ascii=False, sort_keys=True) for e in ordered],
            }
        )
        pq.write_table(table, target, compression=compression)
        exchanges = [e.exchange_ts for e in ordered if e.exchange_ts is not None]
        records.append(
            PartitionRecord(
                dataset=dataset,
                inst_id=event_inst_id(ordered[0]) if event_inst_id(ordered[0]) != NO_INSTRUMENT else None,
                partition_key=key,
                path=str(target.relative_to(base)),
                checksum_sha256=_sha256_of_file(target),
                rows=len(ordered),
                first_ts=min(exchanges) if exchanges else None,
                last_ts=max(exchanges) if exchanges else None,
                written_at=now,
            )
        )
    return records


def read_parquet_events(paths: Sequence[str | Path]) -> list[EventEnvelope]:
    """Relit des partitions Parquet en enveloppes. L'ordre final est ``(available_at, ingest_seq)``."""
    import pyarrow.parquet as pq

    out: list[EventEnvelope] = []
    for path in paths:
        table = pq.read_table(Path(path))
        rows = table.to_pylist()
        for row in rows:
            out.append(
                EventEnvelope(
                    event_id=str(row["event_id"]),
                    event_type=str(row["event_type"]),
                    schema_version=int(row["schema_version"]),
                    source=str(row["source"]),
                    exchange_ts=row["exchange_ts"],
                    receive_ts=row["receive_ts"],
                    available_at=row["available_at"],
                    ingest_seq=int(row["ingest_seq"]),
                    payload_hash=str(row["payload_hash"]),
                    payload=json.loads(str(row["payload_json"])),
                )
            )
    return sorted(out, key=lambda e: (e.available_at, e.ingest_seq))
