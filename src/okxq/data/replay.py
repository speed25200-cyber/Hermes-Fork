"""Replay d'un jeu de données archivé (§32, §34).

L'ordre de rejeu est ``(available_at, ingest_seq)`` : c'est l'ordre dans lequel NOTRE système a appris
les faits, le seul qui permette de reconstruire ce qu'il savait à chaque décision. À horodatage égal, le
départage est stable et documenté (l'ordre de réception enregistré) ; ``admissible_orders`` permet de
rejouer d'autres ordres admissibles quand l'historique ne tranche pas, pour mesurer la sensibilité.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from okxq.data.archive import (
    EVENTS_NAME,
    manifest_path_for,
    read_jsonl_events,
    read_parquet_events,
    verify_dataset_checksum,
)
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope

__all__ = ["ReplaySource", "load_dataset", "replay_order"]


def replay_order(events: Sequence[EventEnvelope]) -> list[EventEnvelope]:
    """Ordre canonique : ``(available_at, ingest_seq)``, puis ``event_id`` pour une stabilité totale."""
    return sorted(events, key=lambda e: (e.available_at, e.ingest_seq, e.event_id))


def load_dataset(directory: str | Path, *, verify: bool = True) -> tuple[dict[str, Any], list[EventEnvelope]]:
    """Charge un jeu de données (JSONL ou Parquet) et son manifeste.

    ``verify=True`` refuse un jeu dont le checksum ne correspond pas : lire un fichier qui a changé
    depuis son écriture rendrait toute reproduction illusoire.
    """
    base = Path(directory)
    manifest_file = manifest_path_for(base)
    if not manifest_file.exists():
        raise DataQualityError(f"manifeste absent : {manifest_file}", path=str(manifest_file))
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DataQualityError(f"manifeste illisible : {exc}", path=str(manifest_file)) from exc
    if not isinstance(manifest, dict):
        raise DataQualityError("manifeste sans objet racine", path=str(manifest_file))

    fmt = str(manifest.get("format", "jsonl"))
    files = [str(f) for f in manifest.get("files", [EVENTS_NAME])]
    if verify:
        verify_dataset_checksum(base, manifest)
    if fmt == "jsonl":
        events: list[EventEnvelope] = []
        for name in files:
            events.extend(read_jsonl_events(base / name))
    elif fmt == "parquet":
        events = read_parquet_events([base / name for name in files])
    else:
        raise DataQualityError(f"format de jeu de données inconnu : {fmt}", path=str(manifest_file))
    return manifest, replay_order(events)


@dataclass(slots=True)
class ReplaySource:
    """Source d'événements pilotant une horloge simulée.

    L'horloge avance à ``available_at`` de chaque événement : une décision prise pendant le rejeu ne peut
    donc pas voir une donnée qui n'était pas encore disponible.
    """

    events: list[EventEnvelope]
    clock: SimulatedClock
    manifest: dict[str, Any]

    @classmethod
    def from_directory(
        cls, directory: str | Path, *, verify: bool = True, clock: SimulatedClock | None = None
    ) -> ReplaySource:
        manifest, events = load_dataset(directory, verify=verify)
        if not events:
            raise DataQualityError("jeu de données vide", path=str(directory))
        return cls(events=events, clock=clock or SimulatedClock(events[0].available_at), manifest=manifest)

    @property
    def first_available_at(self) -> datetime:
        return self.events[0].available_at

    @property
    def last_available_at(self) -> datetime:
        return self.events[-1].available_at

    def __iter__(self) -> Iterator[EventEnvelope]:
        return self.iterate()

    def iterate(self, *, until: datetime | None = None) -> Iterator[EventEnvelope]:
        for env in self.events:
            if until is not None and env.available_at > until:
                return
            if env.available_at > self.clock.now_utc():
                self.clock.set(env.available_at)
            yield env

    def admissible_orders(self, limit: int = 2) -> list[list[EventEnvelope]]:
        """Ordres admissibles alternatifs à horodatage égal (§34 : tester plusieurs ordres possibles).

        Le premier est l'ordre canonique. Le second inverse les groupes d'événements partageant exactement
        le même ``available_at`` — l'historique ne tranche pas entre canaux indépendants.
        """
        orders: list[list[EventEnvelope]] = [list(self.events)]
        if limit <= 1:
            return orders
        groups: dict[datetime, list[EventEnvelope]] = {}
        for env in self.events:
            groups.setdefault(env.available_at, []).append(env)
        reversed_order: list[EventEnvelope] = []
        for stamp in sorted(groups):
            reversed_order.extend(reversed(groups[stamp]))
        orders.append(reversed_order)
        return orders[:limit]
