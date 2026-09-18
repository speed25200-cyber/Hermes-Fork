"""Magasin point-in-time : ce que le système SAVAIT à un instant donné (§34, T14, T15, T18).

Deux principes, et leurs conséquences pratiques :

1. **Une donnée ajoutée plus tard ne change pas une réponse antérieure.** Toute lecture filtre sur
   ``available_at <= cutoff``, donc rejouer une décision passée après avoir collecté davantage de
   données rend exactement le même résultat (T14).
2. **Un événement ancien reçu tard n'était pas connu avant sa réception.** Son heure économique
   (``exchange_ts``) peut être antérieure au cutoff sans qu'il soit disponible : c'est
   ``available_at`` qui décide (T15).

Les versions d'instruments et d'univers suivent la même règle : un actif délisté ou renommé disparaît
de l'univers admissible sans jamais disparaître de la comptabilité — les positions détenues restent
gérables en reduce-only (T18).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from okxq.data.archive import event_inst_id
from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import CausalityError
from okxq.domain.events import EventEnvelope
from okxq.domain.instruments import InstrumentSpec, InstrumentState

__all__ = ["PointInTimeStore", "UniverseVersion"]


@dataclass(frozen=True, slots=True)
class UniverseVersion:
    """Version d'univers datée. ``held_only`` : hors univers mais encore en portefeuille (reduce-only)."""

    universe_version: str
    valid_from: datetime
    eligible: tuple[str, ...]
    held_only: tuple[str, ...] = ()
    criteria: dict[str, Any] = field(default_factory=dict)
    bias_note: str | None = None

    def manageable(self) -> tuple[str, ...]:
        """Instruments que le système doit continuer à suivre : admissibles + détenus."""
        return tuple(sorted(set(self.eligible) | set(self.held_only)))


class PointInTimeStore:
    """Index en mémoire des événements et des versions, interrogeable à un cutoff.

    Les listes sont maintenues triées par ``available_at`` : une insertion hors ordre est acceptée
    (un flux peut livrer en désordre) mais la lecture reste chronologique.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], list[EventEnvelope]] = {}
        self._instrument_versions: dict[str, list[InstrumentSpec]] = {}
        self._universes: list[UniverseVersion] = []

    # --- écriture ---------------------------------------------------------------------------------------

    def add_event(self, envelope: EventEnvelope) -> None:
        key = (envelope.event_type, event_inst_id(envelope))
        series = self._by_key.setdefault(key, [])
        stamp = (envelope.available_at, envelope.ingest_seq)
        if series and (series[-1].available_at, series[-1].ingest_seq) > stamp:
            series.append(envelope)
            series.sort(key=lambda e: (e.available_at, e.ingest_seq))
        else:
            series.append(envelope)

    def add_events(self, envelopes: Iterable[EventEnvelope]) -> None:
        for env in envelopes:
            self.add_event(env)

    def add_instrument_version(self, spec: InstrumentSpec) -> None:
        versions = self._instrument_versions.setdefault(spec.inst_id, [])
        versions.append(spec)
        versions.sort(key=lambda s: s.observed_at)

    def add_universe_version(self, version: UniverseVersion) -> None:
        self._universes.append(version)
        self._universes.sort(key=lambda u: u.valid_from)

    # --- lecture ----------------------------------------------------------------------------------------

    def latest_event(self, event_type: str, inst_id: str | None, cutoff: datetime) -> EventEnvelope | None:
        """Dernier événement de ce type DISPONIBLE au cutoff (``available_at <= cutoff``)."""
        cutoff = ensure_utc(cutoff, field="cutoff")
        series = self._by_key.get((event_type, inst_id or "_"))
        if not series:
            return None
        stamps = [e.available_at for e in series]
        index = bisect_right(stamps, cutoff)
        return series[index - 1] if index > 0 else None

    def events_until(
        self, event_type: str, inst_id: str | None, cutoff: datetime, *, limit: int | None = None
    ) -> list[EventEnvelope]:
        cutoff = ensure_utc(cutoff, field="cutoff")
        series = self._by_key.get((event_type, inst_id or "_"), [])
        stamps = [e.available_at for e in series]
        index = bisect_right(stamps, cutoff)
        window = series[:index]
        return window[-limit:] if limit is not None else list(window)

    def events_between(
        self, event_type: str, inst_id: str | None, start: datetime, cutoff: datetime
    ) -> list[EventEnvelope]:
        start = ensure_utc(start, field="start")
        return [e for e in self.events_until(event_type, inst_id, cutoff) if e.available_at > start]

    def instrument_at(self, inst_id: str, cutoff: datetime) -> InstrumentSpec | None:
        """Version de métadonnées connue au cutoff. Une version postérieure n'éclaire jamais le passé (T04)."""
        cutoff = ensure_utc(cutoff, field="cutoff")
        versions = self._instrument_versions.get(inst_id, [])
        candidates = [s for s in versions if s.observed_at <= cutoff]
        return candidates[-1] if candidates else None

    def instruments_at(self, cutoff: datetime) -> dict[str, InstrumentSpec]:
        out: dict[str, InstrumentSpec] = {}
        for inst_id in self._instrument_versions:
            spec = self.instrument_at(inst_id, cutoff)
            if spec is not None:
                out[inst_id] = spec
        return out

    def tradable_instruments_at(self, cutoff: datetime) -> dict[str, InstrumentSpec]:
        return {k: v for k, v in self.instruments_at(cutoff).items() if v.state is InstrumentState.LIVE}

    def universe_at(self, cutoff: datetime) -> UniverseVersion | None:
        cutoff = ensure_utc(cutoff, field="cutoff")
        candidates = [u for u in self._universes if u.valid_from <= cutoff]
        return candidates[-1] if candidates else None

    def assert_available(self, envelope: EventEnvelope, cutoff: datetime) -> None:
        """Garde de causalité explicite, utile dans les tests et les assertions de pipeline."""
        if envelope.available_at > ensure_utc(cutoff, field="cutoff"):
            raise CausalityError(
                "événement non disponible au cutoff",
                event_id=envelope.event_id,
                available_at=envelope.available_at.isoformat(),
                cutoff=ensure_utc(cutoff).isoformat(),
            )

    # --- diagnostic -------------------------------------------------------------------------------------

    def keys(self) -> list[tuple[str, str]]:
        return sorted(self._by_key)

    def counts(self) -> dict[str, int]:
        return {f"{t}|{i}": len(v) for (t, i), v in sorted(self._by_key.items())}

    def coverage(self, cutoff: datetime) -> dict[str, Any]:
        cutoff = ensure_utc(cutoff, field="cutoff")
        return {
            "cutoff_at": cutoff.isoformat(),
            "series": {f"{t}|{i}": len(self.events_until(t, i, cutoff)) for (t, i) in sorted(self._by_key)},
            "instruments": sorted(self.instruments_at(cutoff)),
            "universe_version": (u.universe_version if (u := self.universe_at(cutoff)) else None),
        }

    @classmethod
    def from_events(cls, events: Sequence[EventEnvelope], *, provenance: str = "replay") -> PointInTimeStore:
        """Construit le magasin depuis un flux, en matérialisant les versions d'instruments rencontrées."""
        from okxq.backtest.market_state import spec_from_instrument_event
        from okxq.runtime.logging import get_logger

        log = get_logger("okxq.point_in_time")
        store = cls()
        for env in events:
            store.add_event(env)
            if env.event_type == "instrument":
                try:
                    spec = spec_from_instrument_event(
                        env.payload, observed_at=env.available_at, provenance=provenance
                    )
                except Exception as exc:
                    # Instrument non supporté (inverse, option, ctMult≠1…) : il n'entre jamais dans
                    # l'univers, et le motif est NOMMÉ — un rejet muet masquerait une divergence de contrat.
                    log.info(
                        "instrument écarté des versions point-in-time",
                        inst_id=str(env.payload.get("inst_id")),
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                store.add_instrument_version(spec)
        return store
